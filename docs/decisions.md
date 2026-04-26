# Technical Decisions

This document records the key architectural and technology decisions made in this project. Each entry follows a structured format to capture not just *what* was decided, but *why* — including the context, alternatives considered, and trade-offs accepted.

---

## Decision 1 — Event-Driven Architecture (Async) vs. Direct Synchronous Calls

### Context

The platform must trigger gamification processing every time a customer performs a PIX transaction. The transaction acceptance path is latency-sensitive (banking standard: < 300ms P99). The gamification engine involves multiple database writes across two stores.

### Problem

How should TransactionService communicate the transaction event to GameService?

### Options Considered

| Option | Description |
|--------|-------------|
| **Synchronous REST call** | TransactionService calls GameService directly after accepting a transaction and waits for a response |
| **Asynchronous messaging via SQS** | TransactionService publishes an event; GameService consumes it independently |
| **Webhook / callback** | GameService registers a callback URL; TransactionService invokes it after the transaction |

### Decision Taken

**Asynchronous messaging via SQS.** TransactionService publishes a `TransactionEvent` to SQS and immediately returns `202 Accepted`. GameService polls SQS independently and processes events at its own pace.

### Why Not Synchronous?

Synchronous communication creates a hard coupling between two operations with fundamentally different SLA requirements:

- **Transaction acceptance** must succeed in milliseconds and be available 99.99% of the time
- **Points calculation** can tolerate seconds of delay and occasional processing failures

Coupling them synchronously means: if GameService is slow, transactions are slow; if GameService is down, transactions fail. This is unacceptable — a rewards engine must never endanger a payment flow.

### Trade-offs Accepted

| Advantage | Cost |
|-----------|------|
| Transaction latency unaffected by gamification | Points are not immediately visible after a transaction (eventual consistency) |
| GameService failures do not fail transactions | Debugging requires correlating event IDs across service logs |
| Natural backpressure via SQS buffering | SQS introduces infrastructure cost and operational overhead |
| Retry semantics built into the messaging layer | Race conditions must be explicitly handled (idempotency, optimistic locking) |

---

## Decision 2 — SQS vs. Kafka for Messaging

### Context

An asynchronous messaging layer is needed between TransactionService and GameService. Multiple messaging technologies exist, each with different characteristics.

### Problem

Which message broker best fits the requirements: reliable at-least-once delivery, automatic retry, dead-letter queue support, and managed operations?

### Options Considered

| Option | Description |
|--------|-------------|
| **AWS SQS** | Fully managed queue service with at-least-once delivery, visibility timeout, and native DLQ |
| **Apache Kafka** | Distributed streaming platform with persistent log, consumer group offsets, and replay capability |
| **RabbitMQ** | Traditional AMQP broker with routing, exchanges, and acknowledgements |

### Decision Taken

**AWS SQS.** The use case is a task queue: process each transaction event once, retry on failure, quarantine unprocessable messages. SQS is the correct tool for this semantic.

### Why Not Kafka?

Kafka's strengths are event streaming (replay arbitrary history, fan-out to multiple consumer groups, high-throughput analytics pipelines). This comes with operational cost: broker clusters, ZooKeeper/KRaft, partition rebalancing, offset management. For a use case that needs simple at-least-once task queue semantics, this complexity adds no value and increases operational burden.

SQS provides exactly what is needed:
- **Fully managed** — no broker infrastructure to operate
- **Built-in DLQ** — failed messages are automatically quarantined after configurable retries
- **Visibility timeout** — prevents double-processing without requiring consumer coordination
- **LocalStack compatibility** — identical API locally and in production

### Trade-offs Accepted

| Advantage | Cost |
|-----------|------|
| No broker infrastructure to operate | Cannot replay arbitrary message history (no persistent log offset) |
| Native DLQ with zero configuration | No native message ordering (FIFO queues exist but are not used here) |
| Scales to millions of messages/day | Max message payload: 256 KB |
| Identical local dev experience with LocalStack | Higher cost at extreme throughput compared to self-hosted Kafka |

---

## Decision 3 — Microservices vs. Monolith

### Context

The system needs to implement three distinct business capabilities: user authentication, financial transaction acceptance, and gamification. These can be packaged as a single application or as separate deployable services.

### Problem

What deployment topology best supports the system's requirements for fault isolation, independent scalability, and long-term evolvability?

### Options Considered

| Option | Description |
|--------|-------------|
| **Monolith** | Single deployable artifact with all three capabilities |
| **Modular monolith** | Single deployment but with enforced module boundaries |
| **Microservices** | Three independently deployable services |

### Decision Taken

**Three separate microservices**, each with its own database, Docker image, and deployment lifecycle.

### Rationale

The three capabilities have **fundamentally different operational characteristics**:

| Dimension | LoginService | TransactionService | GameService |
|-----------|-------------|-------------------|-------------|
| Rate of change | Rare (security is stable) | Occasional | Frequent (campaign rule updates) |
| Scaling profile | Low volume (login is infrequent) | Medium-high (one per transaction) | Matches transaction volume |
| Failure risk | Security incident | Revenue impact | UX degradation |
| Technology fit | Redis for sessions | Stateless HTTP | MongoDB for events |

A monolith would force all three to share the same deployment unit, meaning a gamification rule change requires redeploying authentication code — a risk and operational friction that grows over time.

### Why Not a Modular Monolith?

A modular monolith enforces code boundaries but not operational boundaries. It would not provide:
- Independent scaling (GameService may need more instances without LoginService needing more)
- Independent failure (GameService memory leak would crash the entire monolith)
- Independent technology choices (MongoDB and Redis in the same JVM process is inefficient)

### Trade-offs Accepted

| Advantage | Cost |
|-----------|------|
| Independent deployment and scaling | Three JVMs, three CI pipelines, three Docker images |
| Fault isolation (GameService crash doesn't affect auth) | Distributed tracing required to follow a request across services |
| Technology fit per service | Network calls introduce latency and failure modes absent in a monolith |
| Teams can own services independently | Data consistency across services requires eventual consistency patterns |

---

## Decision 4 — MongoDB vs. Relational Database for GameService Event Store

### Context

GameService receives `TransactionEvent` JSON documents from SQS. These documents need to be stored for audit, idempotency enforcement, and potential replay. The schema of these events may evolve as new fields are added upstream.

### Problem

What database is most appropriate for storing raw event documents in GameService?

### Options Considered

| Option | Description |
|--------|-------------|
| **MongoDB** | Document database with flexible schema and high write throughput |
| **PostgreSQL (relational)** | Schema-enforced relational database |
| **H2 (same database as game state)** | Reuse the existing relational store to avoid a second database |

### Decision Taken

**MongoDB for event documents, H2/JPA for relational game state.** Each database is used for the concern it is best suited for.

### Why MongoDB for Events?

Transaction events are **append-only, schema-flexible records**. Their structure is determined by TransactionService (an external system) and may evolve without notice. Storing them in a relational schema would require migration scripts every time a new field is added — a tight coupling between two independent services' deployment cycles.

MongoDB's document model:
- **Absorbs schema evolution**: new fields in the `TransactionEvent` JSON are stored as-is without migrations
- **Enforces idempotency at the storage layer**: MongoDB `_id` uniqueness natively prevents duplicate event records
- **Optimised for append-only workloads**: no row updates, no lock contention on existing rows

### Why H2/JPA for Game State?

Relational game state (missions, levels, customer progress, benefit redemptions) has well-defined relationships, requires referential integrity, and benefits from optimistic locking. H2/JPA provides:
- `@Version`-based optimistic locking on `CustomerProgress` to prevent lost updates under concurrent processing
- Referential integrity between `MissionCompletion`, `ProcessedEvent`, and `CustomerProgress`
- Familiar query model for rules evaluation (`SELECT missions WHERE minValue <= :amount`)

### Trade-offs Accepted

| Advantage | Cost |
|-----------|------|
| No migration scripts for event schema changes | Two databases in one service increases operational complexity |
| MongoDB `_id` enforces idempotency at storage level | Transactions cannot span MongoDB and H2 (no distributed ACID) |
| High-throughput event inserts without contention | More infrastructure to run locally (MongoDB Docker container) |

---

## Decision 5 — Redis in LoginService for Sessions and Rate Limiting

### Context

LoginService must store session context after authentication so that other services (TransactionService) can validate sessions without querying the user database. It must also prevent brute-force login attacks.

### Problem

Where should active sessions be stored, and how should rate limiting be enforced in a horizontally scalable deployment?

### Options Considered

| Option | Description |
|--------|-------------|
| **Redis** | In-memory key-value store with TTL support and Lua scripting |
| **Relational database (H2/PostgreSQL)** | Store sessions as rows with expiry column |
| **JWT self-contained sessions** | Embed all session data in the JWT; no server-side session store |
| **Sticky sessions (load balancer)** | Route each client to the same instance |

### Decision Taken

**Redis for both session storage and distributed rate limiting.**

### Why Redis for Sessions?

Session validation is on the **hot path of every transaction** — TransactionService calls `/me` synchronously on every request. Session lookups must be sub-millisecond. Redis:
- Sub-millisecond reads via in-memory storage
- Automatic TTL expiry — sessions expire at 5 minutes without cleanup jobs
- Horizontally shared — all LoginService instances read the same Redis instance, making the service stateless and scalable

### Why Redis for Rate Limiting?

A distributed rate limiter must be **atomic across concurrent requests** and work correctly when multiple LoginService instances run in parallel. Redis executes Lua scripts atomically — the `INCR + EXPIRE` operation runs as a single transaction, preventing race conditions that could allow a burst of requests to bypass the limit.

**Fail-open behavior**: if Redis is unavailable, the rate limiter allows requests through. This is a deliberate trade-off: a Redis outage should not lock all customers out of the app. Availability is prioritized over rate limiting robustness in this failure scenario.

### Why Not JWT Self-Contained Sessions?

Self-contained JWTs (stateless tokens) cannot be revoked before expiry. In a banking context, logout must invalidate the session immediately. Server-side session storage in Redis allows a `DELETE session:<sessionId>` to revoke a session in real time.

### Trade-offs Accepted

| Advantage | Cost |
|-----------|------|
| Sub-millisecond session reads | Additional infrastructure dependency (Redis) |
| Automatic session expiry via TTL | Redis unavailability affects session lookups |
| Atomic distributed rate limiting | Rate limiter fails open on Redis outage (by design) |
| Stateless LoginService instances | Increased infrastructure complexity |

---

## Decision 6 — Resilience4j Circuit Breaker on LoginService Calls

### Context

TransactionService calls LoginService `/me` synchronously on every transaction. This is the only synchronous inter-service call in the architecture and is therefore on the critical path.

### Problem

How should TransactionService protect itself against LoginService degradation or unavailability?

### Options Considered

| Option | Description |
|--------|-------------|
| **Resilience4j circuit breaker + retry** | Automatic failure detection, fail-fast on open circuit, exponential backoff retry |
| **Timeout only** | Simple connection and read timeout without circuit breaking |
| **Bulkhead** | Limit concurrent calls to LoginService to prevent thread exhaustion |
| **No protection** | Direct HTTP calls without resilience patterns |

### Decision Taken

**Resilience4j circuit breaker combined with retry**, configured with:
- Circuit opens after 50% failure rate in a 10-call sliding window
- Open state lasts 30 seconds before attempting recovery
- 3 retry attempts with 500ms base + exponential backoff
- No retry on `BusinessException` or `UnauthorizedException` (these are not transient)

### Why a Circuit Breaker?

Without a circuit breaker, a degraded LoginService causes threads to pile up waiting for timeouts. Once the thread pool is exhausted, TransactionService itself becomes unresponsive — a cascade failure. The circuit breaker detects degradation early and fails-fast, preserving TransactionService's resources for requests that can succeed.

### Trade-offs Accepted

| Advantage | Cost |
|-----------|------|
| Prevents cascade failures from LoginService degradation | Circuit may trip on transient issues, briefly rejecting valid requests |
| Retry absorbs transient network blips | Retry adds latency on retried calls |
| Clear failure semantics (503 with reason) | Requires tuning of thresholds for the specific traffic pattern |

---

## Decision 7 — Optimistic Locking on CustomerProgress

### Context

GameService may process multiple SQS messages for the same customer concurrently (e.g., a burst of transactions). The `CustomerProgress` entity accumulates points across events. Without coordination, concurrent updates would cause lost writes.

### Problem

How should concurrent updates to `CustomerProgress` be coordinated?

### Options Considered

| Option | Description |
|--------|-------------|
| **Optimistic locking (`@Version`)** | Detect conflicts at commit time; retry on collision |
| **Pessimistic locking (`SELECT FOR UPDATE`)** | Hold a database row lock during the entire processing window |
| **Serialise per customer** | Queue messages per customer ID to prevent concurrency |

### Decision Taken

**Optimistic locking via JPA `@Version`.** A `version` column is incremented on every update. If two threads attempt to commit an update from the same version, the second throws `OptimisticLockException` and retries.

### Why Not Pessimistic Locking?

Pessimistic locking holds a database row lock for the entire message processing window (parse → MongoDB check → H2 checks → rules evaluation → save). This would serialize all processing for a given customer at the database level, eliminating concurrency. Given that contention per customer is expected to be low (a customer rarely sends multiple transactions within the same millisecond window), optimistic locking is more efficient: it allows concurrent processing and only incurs a retry cost in the rare case of collision.

### Trade-offs Accepted

| Advantage | Cost |
|-----------|------|
| No database-level row locks held during processing | Requires retry logic for `OptimisticLockException` |
| Higher throughput under low contention | Under heavy contention per customer, retry storms are possible |
| Simple JPA annotation | Must ensure the entity is reloaded (not cached) before retry |

---

## Decision 8 — Java 21 Virtual Threads

### Context

Both LoginService and GameService handle high-concurrency I/O workloads: concurrent HTTP requests (LoginService) and concurrent SQS message processing (GameService).

### Problem

How should high I/O concurrency be achieved without reactive programming complexity or OS thread limits?

### Decision Taken

**Java 21 Virtual Threads** (`spring.threads.virtual.enabled: true`). GameService additionally dispatches each SQS message to a dedicated virtual thread via a `virtualThreadExecutor`.

### Rationale

Virtual threads are lightweight (few KB vs ~1MB for platform threads) and managed by the JVM, not the OS. This allows thousands of concurrent I/O operations (HTTP calls, database queries, SQS operations) without exhausting OS thread limits or requiring non-blocking reactive code.

For GameService, this means each incoming SQS message can be processed in its own isolated virtual thread — simpler code, better isolation, and high concurrency without the complexity of reactive frameworks (WebFlux, RxJava).

### Trade-offs Accepted

| Advantage | Cost |
|-----------|------|
| High I/O concurrency without reactive code | Virtual threads are new in Java 21 — requires JDK 21+ |
| Simpler code than reactive frameworks | Pinning risk: synchronized blocks pin virtual threads to OS threads |
| Low memory overhead per concurrent operation | Not a benefit for CPU-bound workloads (no improvement over platform threads) |

---

## Decision 9 — Dual Idempotency in GameService

### Context

SQS provides **at-least-once delivery** semantics — the same message may be delivered more than once (e.g., after a consumer crash between processing and message deletion). If processed twice, a customer would receive double points.

### Problem

How should GameService guarantee that each transaction event is processed exactly once?

### Decision Taken

**Dual-layer idempotency**:
1. **MongoDB layer** — check `GameEventDocument` by `eventId` before any processing
2. **H2 layer** — check `ProcessedEvent` by `eventId` before applying business rules

### Why Two Layers?

Each layer protects against a different failure mode:

- The **MongoDB layer** catches redeliveries before any state is modified. If the event document exists, the message is acknowledged and discarded immediately.
- The **H2 layer** catches a narrower race: if GameService saved the MongoDB document but crashed before saving the `ProcessedEvent`. On redelivery, the MongoDB check would miss this case (the document exists), but the H2 check catches it.

Together, they guarantee exactly-once processing even under the most pessimistic failure scenarios (crash between any two operations in the processing pipeline).

### Trade-offs Accepted

| Advantage | Cost |
|-----------|------|
| Exactly-once processing under all failure modes | Two database lookups on every message (minimal latency cost) |
| Each layer independently useful for debugging | Two databases means no atomic transaction spanning both layers |
| MongoDB `_id` provides natural audit trail | Slightly more complex code to maintain both idempotency checks |
