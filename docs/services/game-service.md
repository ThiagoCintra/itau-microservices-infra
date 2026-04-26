# GameService

## Responsibility

GameService is the **gamification engine** of the Itaú microservices platform. It operates entirely asynchronously — it does not expose any HTTP endpoints for transaction ingestion. Instead, it continuously consumes events from an AWS SQS queue and applies gamification business rules.

Its responsibilities are:
- Consume `TransactionEvent` messages from SQS (long-polling)
- Enforce idempotency at two layers (MongoDB + H2)
- Apply monthly point resets per customer
- Evaluate and award mission completions based on transaction amount ranges
- Recalculate customer level based on accumulated points
- Track benefit redemptions per customer per month
- Forward irrecoverable messages to a Dead Letter Queue (DLQ)

---

## Technologies

| Technology               | Role                                                         |
|--------------------------|--------------------------------------------------------------|
| Spring Boot              | Application framework                                        |
| Spring Data JPA (H2)     | Relational persistence: customer progress, missions, levels, completions, redemptions |
| Spring Data MongoDB      | Event log persistence: `GameEventDocument` (audit + idempotency) |
| AWS SDK v2 (SQS)         | SQS consumer with long-polling and visibility timeout control |
| LocalStack               | Local SQS simulation for development                         |
| Java 21 Virtual Threads  | Concurrent SQS message processing                            |
| Jackson                  | JSON deserialization of `TransactionEvent`                   |
| Micrometer + Prometheus  | Health and metrics exposure                                  |

---

## No HTTP Ingestion Endpoint

GameService does **not** expose a `POST /transactions` endpoint. This is a deliberate design choice:

- Gamification is not on the critical transaction path
- The SQS consumer pattern decouples processing from ingestion
- Failure in GameService never propagates back to the customer

The service exposes only actuator endpoints (`/actuator/health`, `/actuator/metrics`, `/actuator/prometheus`).

---

## Main Classes

| Class | Package | Role |
|-------|---------|------|
| `GameServiceApplication` | root | Spring Boot entry point |
| `SqsConsumer` | `infrastructure.sqs` | SQS polling loop, message dispatching, DLQ routing |
| `SqsConfig` | `infrastructure.sqs` | AWS SQS client configuration (LocalStack compatible) |
| `StartupRunner` | `infrastructure.consumer` | `ApplicationRunner` that starts `SqsConsumer.startPolling()` |
| `GameApplicationService` | `application` | Orchestrates the full gamification processing pipeline |
| `GamificationService` | `service` | Spring `@Service` bridge — constructs `GameApplicationService`, exposes `processEvent()` |
| `MissionService` | `service` | Mission eligibility and completion logic |
| `LevelService` | `service` | Level calculation from accumulated points |
| `RedemptionService` | `service` | Monthly benefit redemption status check |
| `BootstrapDataInitializer` | `infrastructure` | Seeds default missions and level rules on first startup |
| `CustomerProgress` | `domain` | JPA entity: customer's accumulated points, level, lastReset |
| `Mission` | `domain` | JPA entity: mission definition with amount range and points |
| `MissionCompletion` | `domain` | JPA entity: tracks which missions a customer has completed |
| `LevelRule` | `domain` | JPA entity: maps minimum points threshold to level number |
| `BenefitRedemption` | `domain` | JPA entity: tracks monthly benefit redemptions |
| `ProcessedEvent` | `domain` | JPA entity: idempotency marker by `eventId` |
| `GameEventDocument` | `infrastructure.persistence.mongo` | MongoDB document: raw event log for audit and first idempotency check |
| `CustomerProgressRepository` | `infrastructure.persistence` | JPA repository with `findByCustomerIdForUpdate` |
| `MissionRepository` | `infrastructure.persistence` | JPA repository, `findAllByActiveTrue()` |
| `MissionCompletionRepository` | `infrastructure.persistence` | JPA repository, checks completion by customerId + missionId |
| `LevelRuleRepository` | `infrastructure.persistence` | JPA repository, ordered by `minPoints ASC` |
| `BenefitRedemptionRepository` | `infrastructure.persistence` | JPA repository, checks monthly redemption |
| `ProcessedEventRepository` | `infrastructure.persistence` | JPA repository, `existsByEventId()` |
| `GameEventRepository` | `infrastructure.persistence.mongo` | Spring Data MongoDB repository |

---

## Internal Flow

```
StartupRunner.run()
      │
      └── SqsConsumer.startPolling()  [virtual thread — runs forever]
              │
              loop:
              ├── sqsClient.receiveMessage(queueUrl, maxMessages=10, waitTime=20s)
              │
              └── for each message:
                  executor.submit(() -> processMessage(m))  [virtual thread]
                        │
                        ├── objectMapper.readValue(body) → TransactionEvent
                        │
                        ├── gameEventRepository.findById(eventId)  [MongoDB]
                        │   → if found: deleteMessage(m), return
                        │
                        ├── gameEventRepository.save(GameEventDocument)  [MongoDB]
                        │
                        ├── gamificationService.processEvent(event)
                        │       │
                        │       └── GameApplicationService.processTransactionEvent(event)
                        │               │
                        │               ├── processedEventRepository.existsByEventId()  [H2]
                        │               │   → if found: return (idempotency)
                        │               │
                        │               ├── filter: type != "PIX" → return
                        │               │
                        │               ├── customerProgressRepository.findByCustomerIdForUpdate()
                        │               │   → create new CustomerProgress if not found
                        │               │
                        │               ├── monthly reset check
                        │               │   → if different month: reset points=0, level=1
                        │               │
                        │               ├── redemptionService.hasRedeemedThisMonth()
                        │               │   → if redeemed: save progress, return
                        │               │
                        │               ├── missionService.evaluateAndApply(event, progress)
                        │               │   └── for each active Mission:
                        │               │       ├── eligibleForMission(amount, mission)?
                        │               │       ├── missionCompletionRepo.existsByCustomerIdAndMissionId()?
                        │               │       └── if new completion: progress.totalPoints += mission.points
                        │               │           save MissionCompletion
                        │               │
                        │               ├── levelService.calculateLevel(totalPoints)
                        │               │   └── select highest level where minPoints <= totalPoints
                        │               │       → progress.level = calculated level
                        │               │
                        │               ├── customerProgressRepository.save(progress)  [H2, @Version]
                        │               │
                        │               └── processedEventRepository.save(ProcessedEvent)  [H2]
                        │
                        ├── deleteMessage(m)  ← acknowledge to SQS
                        │
                        └── on failure:
                            handleFailedMessage(m)
                            ├── if receiveCount > maxReceiveCount (5):
                            │   ├── sqsClient.sendMessage(dlqUrl, body)
                            │   └── deleteMessage(m)
                            └── else:
                                sqsClient.changeMessageVisibility(
                                    min(60 * receiveCount, 43200) seconds)
```

---

## Gamification Rules Detail

### Mission Eligibility

A customer becomes eligible for a mission when:
1. The transaction type is `PIX`
2. The transaction `amount` falls within `[mission.minValue, mission.maxValue]`
3. The customer has **not** previously completed this mission (no `MissionCompletion` record exists)

Missions are stored in H2 and bootstrapped on first startup:

| Mission    | Min Value (BRL) | Max Value (BRL) | Points |
|------------|-----------------|-----------------|--------|
| PIX Small  | 0.01            | 1,000.00        | 5      |
| PIX Medium | 1,000.00        | 9,999.00        | 10     |
| PIX Large  | 10,000.00       | unlimited        | 20     |

### Level Calculation

Levels are evaluated by scanning `LevelRule` entries ordered by `minPoints ASC` and selecting the highest level whose threshold is ≤ `totalPoints`:

| Level | Minimum Points |
|-------|---------------|
| 1     | 0             |
| 2     | 100           |
| 3     | 500           |
| 4     | 1,000         |
| 5     | 2,000         |

### Monthly Reset

Triggered automatically when `lastReset` (stored in `CustomerProgress`) belongs to a different calendar month than the current processing timestamp:
- `totalPoints` → 0
- `level` → 1
- `lastReset` → current timestamp

### Benefit Redemption Guard

If `BenefitRedemptionRepository` finds a redemption record for the customer in the current month, further event processing is skipped for that customer. Points are already tracked but no duplicate redemption is triggered.

---

## Dual Idempotency Architecture

| Layer | Check | Store | Purpose |
|-------|-------|-------|---------|
| **1. SQS message layer** | `gameEventRepository.findById(eventId)` | MongoDB | Fastest check; prevents reprocessing even before any JPA writes |
| **2. Business logic layer** | `processedEventRepository.existsByEventId(eventId)` | H2 | Ensures gamification rules are applied exactly once, even if MongoDB write fails |

This two-layer approach handles the following failure scenarios:
- SQS redelivers a message after a crash between delete and MongoDB save → Layer 1 catches it on retry
- MongoDB insert succeeds but H2 rule application fails → Layer 2 catches it on retry
- Both layers persisted but SQS delete failed → Layer 1 catches the redeliver

---

## Error Handling and DLQ

| Scenario | Behaviour |
|----------|-----------|
| Transient error (e.g., H2 lock timeout) | Visibility timeout extended; SQS retries automatically |
| Repeated failures (> 5 receive attempts) | Message forwarded to `transactions-dlq`; removed from main queue |
| Deserialization failure | Logged as `WARN`; message goes through DLQ flow |
| Unexpected exception | Logged as `ERROR`; same DLQ flow |

---

## Integrations

| Integration | Direction | Protocol | Notes |
|-------------|-----------|----------|-------|
| AWS SQS | Inbound (consumer) | AWS SDK v2 | Polls `transactions` queue |
| AWS SQS DLQ | Outbound (producer) | AWS SDK v2 | Forwards irrecoverable messages |
| MongoDB | Outbound | Spring Data MongoDB | Event log / idempotency |
| H2 (JPA) | Outbound | JDBC | Game state (progress, missions, levels) |

---

## Configuration

Key configuration properties (`application.yaml`):

```yaml
server:
  port: 8082

spring:
  data:
    mongodb:
      host: ${SPRING_DATA_MONGODB_HOST:localhost}
      port: 27017
      database: game_db
  datasource:
    url: jdbc:h2:mem:game_db;DB_CLOSE_DELAY=-1

aws:
  sqs:
    queue-url: ${SQS_QUEUE_URL:http://localhost:4566/000000000000/transactions}
    dlq-url: ${SQS_DLQ_URL:http://localhost:4566/000000000000/transactions-dlq}
    region: ${AWS_REGION:us-east-1}
    endpoint: ${AWS_ENDPOINT:http://localhost:4566}

app:
  sqs:
    poll-interval-ms: 500
    max-messages: 10
    wait-time-seconds: 20
    max-receive-count: 5
```
