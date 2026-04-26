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

### Why This Boundary?

GameService is the system of record for **customer engagement data** — a domain that evolves at a fundamentally different pace than transaction processing or authentication.

Key consequences of this boundary:
- **Mission rules can be added or changed** (insert a new `Mission` row) without touching TransactionService or LoginService
- **Level thresholds can be reconfigured** without redeploying anything on the transaction path
- **GameService can be taken offline for maintenance** and SQS will buffer events; when it comes back online, it processes the queue with no data loss
- **A bug in gamification rules** (e.g., awarding too many points) is confined to this service and does not affect payment processing

This boundary is what makes the architecture genuinely resilient, not just technically asynchronous.

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

GameService does **not** expose a `POST /transactions` endpoint. This is a deliberate architectural choice, not an omission:

- **Gamification is not on the critical path**: there is no reason for TransactionService to wait for a gamification response. Making GameService an HTTP server for transactions would tightly couple its availability to the transaction flow.
- **SQS as the ingestion contract**: the only supported way to deliver events to GameService is via SQS. This makes the ingestion contract explicit and durable — events are persisted in the queue and cannot be lost if GameService is temporarily unavailable.
- **Failure isolation is preserved**: if GameService crashes, SQS continues to accept and hold events. When GameService recovers, it processes the backlog. No transactions are lost or rejected.

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

SQS provides **at-least-once delivery**: the same message may be delivered more than once, especially if the consumer crashes between processing and message deletion. Without idempotency guarantees, a customer could receive double points.

GameService implements **two independent idempotency layers** to handle all possible crash scenarios:

| Layer | Check | Store | Failure scenario covered |
|-------|-------|-------|--------------------------|
| **1. SQS message layer** | `gameEventRepository.findById(eventId)` | MongoDB | Redelivery before any state is modified |
| **2. Business logic layer** | `processedEventRepository.existsByEventId(eventId)` | H2 | Redelivery after MongoDB write but before H2 commit |

**Why two layers instead of one?**

A single idempotency layer creates a window of vulnerability at the boundary between the two databases. Consider:

1. MongoDB document saved → process crashes before H2 commit
2. SQS redelivers the message
3. Layer 1 (MongoDB) finds the document → assumes already processed → skips
4. But H2 was never updated → customer progress is lost

Layer 2 catches exactly this scenario. Together, the two layers close all windows:

| Crash point | Recovery mechanism |
|------------|-------------------|
| Before MongoDB save | Layer 1 misses → Layer 2 misses → full reprocessing (safe) |
| After MongoDB, before H2 save | Layer 1 finds document → skips at SQS layer? No — Layer 1 saves and continues; Layer 2 catches on retry |
| After H2 save, before SQS delete | Layer 1 finds document → skip — correct, already processed |
| After all saves, SQS delete fails | Same as above — idempotent |

---

## Error Handling and DLQ

The DLQ is not just an error log — it is a **resilience mechanism** that prevents a single bad message from blocking the queue indefinitely.

| Scenario | Behaviour | Why |
|----------|-----------|-----|
| Transient error (H2 lock timeout, network blip) | Visibility timeout extended exponentially (`60s × receiveCount`) | Gives the system time to recover without storm of retries |
| Repeated failures (> 5 receive attempts) | Message forwarded to `transactions-dlq`; removed from main queue | Isolates unprocessable messages; healthy messages continue processing |
| Deserialization failure | Logged as `WARN`; goes through DLQ flow | A malformed message should not block the queue |
| Unexpected exception | Logged as `ERROR`; DLQ flow | Unknown failures are quarantined for investigation |

Messages in the DLQ are preserved indefinitely for analysis and can be replayed after the root cause is fixed.

---

## Integrations

| Integration | Direction | Protocol | Notes |
|-------------|-----------|----------|-------|
| AWS SQS | Inbound (consumer) | AWS SDK v2 | Long-polls `transactions` queue — the only way events enter this service |
| AWS SQS DLQ | Outbound (producer) | AWS SDK v2 | Forwards irrecoverable messages after `max-receive-count` retries |
| MongoDB | Outbound | Spring Data MongoDB | Event log / first idempotency layer |
| H2 (JPA) | Outbound | JDBC | Game state (progress, missions, levels) / second idempotency layer |

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
