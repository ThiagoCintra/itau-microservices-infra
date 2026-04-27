# Architecture — Itaú Microservices Platform

## Overview

The platform is composed of **three independently deployable microservices** that communicate through a combination of synchronous REST calls (for authentication) and asynchronous messaging (for gamification) via AWS SQS.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                            Itaú Microservices Platform                        │
│                                                                                │
│  ┌─────────────┐  JWT    ┌──────────────────┐  SQS Event   ┌──────────────┐  │
│  │             │◄───────►│                  │─────────────►│              │  │
│  │ LoginService│  /me    │TransactionService│              │  GameService │  │
│  │  :8081      │         │     :8080        │              │    :8082     │  │
│  └──────┬──────┘         └──────────────────┘              └──────┬───────┘  │
│         │                                                          │          │
│     Redis                    LocalStack SQS                   H2 + MongoDB   │
│   (sessions)               (transactions queue)               (game state)   │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Architectural Pattern: Event-Driven Microservices

### Why Event-Driven?

The core architectural decision is to **decouple transaction processing from gamification logic** through an asynchronous, event-driven approach. This is not a coincidental or fashionable choice — it is the direct consequence of two fundamental requirements:

1. **Transaction confirmation must be fast and reliable.** Banking customers expect their PIX transfer to be confirmed in milliseconds. Any gamification logic added to the critical path risks degrading this experience.

2. **Gamification must never block or fail a financial transaction.** A bug, slowdown, or outage in a rewards engine cannot be allowed to prevent a customer from making a payment.

Event-driven architecture solves both requirements simultaneously: the transaction is accepted and acknowledged before gamification begins, and the two concerns are physically separated by a durable message queue.

#### Business Value

- **Customer experience**: low-latency transaction confirmations regardless of how complex the gamification rules become
- **Business agility**: gamification campaigns can be updated, A/B tested, or rolled back without touching transaction infrastructure
- **Risk isolation**: gamification failures have zero blast radius on the revenue-generating transaction flow

---

## Why NOT Synchronous REST for Gamification?

A naive implementation would have `TransactionService` call `GameService` directly after receiving a transaction:

```
Client → TransactionService → GameService (sync) → response
```

This pattern is technically simple but architecturally dangerous in a banking context:

| Problem | Impact |
|---------|--------|
| **Latency coupling** | If GameService is slow (DB contention, GC pause), every transaction slows down |
| **Availability coupling** | GameService downtime = transaction failures — a points engine brings down payments |
| **Tight deployment coupling** | You cannot deploy or restart GameService without risking transaction errors |
| **No retry semantics** | On a transient failure, you must either fail the transaction or silently skip gamification |
| **No backpressure control** | A burst of transactions immediately overwhelms GameService with no buffer |
| **Cascading failures** | A degraded GameService causes thread exhaustion in TransactionService, which then also degrades |

**The fundamental issue is that synchronous coupling makes two independent business concerns share the same failure domain.** A banking transaction and a points calculation have very different SLA requirements and risk profiles. Forcing them into the same request/response cycle is an architectural mistake.

### The Asynchronous Solution

By publishing a `TransactionEvent` to SQS and having GameService consume it independently:

- Transactions are **accepted immediately** — P99 latency stays low regardless of gamification load
- Gamification **failures are isolated** — transactions always succeed
- GameService can be **scaled, restarted, or upgraded** without affecting transactions
- **Natural buffering**: SQS holds events during GameService downtime and processes them in order when it recovers
- **Retry semantics**: failed messages are retried with exponential backoff and routed to a DLQ after max retries

---

## Messaging: AWS SQS

The messaging layer uses **Amazon Simple Queue Service (SQS)** via LocalStack in local/test environments. SQS was chosen over alternatives like Kafka because the use case calls for **reliable task queue semantics**, not event streaming.

### Why SQS Instead of Kafka?

Kafka excels at high-throughput event streaming where you need to replay history or maintain consumer group offsets across many topics. For this use case — reliably processing each gamification event exactly once with automatic retry — SQS is a better architectural fit:

- **Simpler ops model**: SQS is fully managed; no broker clusters to provision, monitor, or tune
- **Native DLQ**: failed messages automatically route to a Dead Letter Queue, no custom error handling needed
- **At-least-once delivery**: SQS guarantees delivery even if consumers crash, with configurable retry
- **Visibility timeout**: prevents double-processing by making in-flight messages invisible to other consumers

### Queue Configuration

| Queue              | Purpose                                                 |
|--------------------|---------------------------------------------------------|
| `transactions`     | Main queue — events produced by TransactionService      |
| `transactions-dlq` | Dead Letter Queue — events that failed after max retries |

### Why Use a Dead Letter Queue?

Without a DLQ, a message that consistently fails processing (e.g., due to a data corruption bug) would retry forever, blocking the queue and potentially preventing healthy messages from being processed. The DLQ acts as a quarantine:

- Failed messages are isolated automatically after `max-receive-count` (5) retries
- Engineers can inspect DLQ contents to diagnose and fix the root cause
- Once fixed, messages can be replayed from the DLQ without any data loss

### Retry and Backpressure Strategy

GameService applies **exponential backoff on visibility timeout** when processing fails. Instead of immediately releasing the message for re-processing (which would cause a storm of retries), the visibility timeout is extended:

```
visibility_timeout = 60s × receiveCount  (capped at 12h)
```

This gives the system time to recover from transient failures (network blips, temporary DB unavailability) without overwhelming downstream systems.

### Message Flow

```
TransactionService
    │
    └──► SQS `transactions` queue
              │
              └──► GameService (long-polling consumer, up to 20s wait)
                        │
                        ├── Parse JSON
                        ├── Check idempotency (MongoDB + H2)
                        ├── Apply gamification rules
                        └── Delete message on success
                            (or extend visibility timeout on failure → DLQ after 5 attempts)
```

---

## Service Separation

### Why Three Separate Services?

Splitting into three services reflects **three distinct bounded contexts** with different:
- Rate of change (gamification rules change often; auth changes rarely)
- Scaling requirements (transaction volume ≫ login volume at peak)
- Technology fit (MongoDB for gamification events; Redis for sessions)
- Risk profiles (auth bugs are security incidents; gamification bugs are UX issues)

A monolith would force all three to share the same deployment unit, database, and failure domain — eliminating the independent scalability, deployability, and fault isolation that make this architecture resilient.

### LoginService

**Core responsibility**: authentication and session management — the only service that handles credentials.

- Issues **JWT tokens** embedding: `customerId`, `channel`, `sessionId`, `role`
- Stores active sessions in **Redis** for fast lookup by other services
- Exposes `/me` endpoint as the single source of truth for session validity
- Enforces **rate limiting** to protect against brute-force attacks
- **Does not** know about transactions or gamification — its concern ends at "is this user who they say they are?"

### TransactionService

**Core responsibility**: transaction acceptance — the revenue-critical entry point.

- Validates JWT, channel (`MOBILE` only), and transaction fields
- Calls LoginService `/me` to confirm the session is still active (the only synchronous inter-service call justified by the security requirement)
- Publishes a `TransactionEvent` to SQS and returns `202 Accepted` immediately
- Protected by **Resilience4j circuit breaker + retry** on LoginService calls
- **Does not** process gamification or know about points/levels/missions

### GameService

**Core responsibility**: gamification processing — the system of record for customer engagement.

- Runs a **background SQS polling loop** (virtual threads, long-polling) — no synchronous trigger needed
- Applies mission rules, level calculations, and benefit tracking
- Uses **dual idempotency** (MongoDB + H2) to handle SQS at-least-once delivery
- Completely **stateless from the HTTP perspective** — no REST endpoint for transaction ingestion
- **Does not** know how transactions were validated or how JWTs were issued

### Why These Boundaries Matter

Each service boundary is drawn along **data ownership lines**: LoginService owns credentials and sessions; TransactionService owns the transaction record; GameService owns gamification state. No service reads directly from another service's database. This is the foundation of true service independence.

---

## MongoDB in GameService

### Why a Document Database for Gamification?

GameService uses **MongoDB for event storage** (the `GameEventDocument`) alongside H2 for relational game state (`CustomerProgress`, `MissionCompletion`, etc.).

**The fundamental reason**: transaction events are schema-less data. A `TransactionEvent` published today may carry different fields six months from now (e.g., a new merchant category, a campaign tag). Storing these events in a rigid relational schema would require database migrations every time the upstream contract evolves.

MongoDB's document model solves this:
- **No migration scripts** for event schema evolution — new fields are simply present or absent
- **Append-only event log** — documents are inserted and never updated, a natural fit for the document model
- **Idempotency enforcement** at the storage layer — MongoDB `_id` uniqueness prevents duplicate event records
- **High write throughput** for event ingestion without lock contention

### Why Not Use MongoDB for All Game State?

Relational game state (missions, levels, customer progress) has well-defined relationships and requires ACID transactions. H2/JPA provides:
- **Optimistic locking** via `@Version` to prevent lost updates under concurrent processing
- **Referential integrity** between `CustomerProgress`, `MissionCompletion`, and `ProcessedEvent`
- **Familiar query model** for rules-based logic (find missions where amount is in range)

Using the right database for each concern within a single service is a pragmatic trade-off, accepted explicitly.

---

## Asynchronous Processing

### Why Not Process Gamification in the Same Transaction?

Gamification processing in GameService involves multiple database writes (MongoDB insert, H2 updates across several tables). Processing this within the same database transaction as the original financial transaction would:

1. **Extend the lock window** on financial tables, reducing throughput
2. **Create a distributed transaction** spanning two databases — an extremely fragile pattern with no standard rollback semantics across MongoDB and H2
3. **Couple the success of the transaction to the success of points calculation** — unacceptable from a reliability standpoint

By processing asynchronously:
- The financial transaction commits quickly and cleanly
- Gamification runs as a separate, independent unit of work
- Failure in gamification never rolls back or blocks financial processing

### Impact on User Experience

The user receives a `202 Accepted` response **before** any points are calculated. From the user's perspective, the transaction is confirmed immediately. Points may appear seconds later (after SQS delivery and processing), but this is an intentional trade-off:

- Perceived latency is lower (the app responds faster)
- The eventual-consistency lag for points is unnoticeable in practice (typically < 1s under normal load)
- The alternative — making the user wait for gamification — would degrade the experience without any meaningful benefit

---

## Trade-offs

This architecture offers significant advantages but comes with real costs that must be managed explicitly.

### Advantages

| Advantage | Description |
|-----------|-------------|
| **Fault isolation** | GameService failure does not affect transaction acceptance |
| **Independent scalability** | Each service scales according to its own load profile |
| **Independent deployability** | Teams can release gamification updates without coordination |
| **Natural buffering** | SQS absorbs traffic spikes without overwhelming GameService |
| **Retry semantics** | Failed events are retried automatically with no manual intervention |

### Accepted Costs

| Cost | Mitigation |
|------|-----------|
| **Eventual consistency** | Points are not instantly visible; acceptable for a non-financial reward |
| **Distributed debugging** | Correlating a transaction to its gamification result requires event IDs across services; mitigated by structured logging with `eventId` as a correlation key |
| **Operational complexity** | Three services, three databases, one message broker — more infrastructure than a monolith; mitigated by Docker Compose and documented runbooks |
| **Idempotency responsibility** | SQS at-least-once delivery requires explicit idempotency handling; implemented via dual-layer check (MongoDB + H2) |
| **No global transactions** | Failure after SQS publish but before GameService processes means gamification may be delayed but will eventually complete; the transaction is not rolled back |

### What This Architecture Optimises For

This architecture explicitly optimises for:
1. **Transaction reliability** — the core financial operation never fails due to secondary concerns
2. **Gamification flexibility** — the rules engine can evolve independently without deployment risk
3. **Operational independence** — services can be operated, monitored, and scaled by separate teams

It explicitly accepts eventual consistency in the gamification layer as a worthwhile trade-off for these properties.

---

## Scalability

| Concern | Solution |
|---------|----------|
| High transaction volume | TransactionService is stateless and horizontally scalable |
| High gamification load | GameService can be scaled independently; SQS buffers excess load |
| Session storage | Redis allows all LoginService instances to share session state |
| Concurrent message processing | GameService uses Java 21 Virtual Threads for concurrent event processing |
| Database contention | `CustomerProgress` uses optimistic locking (`@Version`) to prevent lost updates |

---

## Infrastructure Components

| Component     | Role                                     | Local Replacement |
|---------------|------------------------------------------|-------------------|
| AWS SQS       | Message queue between TS and GS          | LocalStack        |
| Redis         | Session storage + rate limiting for LS   | Docker Redis      |
| H2 Database   | Relational store for LS and GS (local)   | In-memory         |
| MongoDB       | Event store / audit log for GS           | Docker MongoDB    |
| WireMock      | Mock for LoginService in TS integration tests | Docker WireMock |
