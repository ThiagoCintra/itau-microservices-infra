# Technical Decisions

This document explains the key architectural and technology choices made in this project, including the trade-offs considered.

---

## 1. Why AWS SQS for Messaging?

### Decision
Use SQS as the message broker between TransactionService and GameService.

### Rationale
- **Managed service**: no broker infrastructure to operate (vs Kafka/RabbitMQ self-hosted)
- **At-least-once delivery**: SQS guarantees messages are delivered and retried until explicitly deleted
- **Built-in DLQ support**: failed messages are automatically routed to a Dead Letter Queue after a configurable number of retries
- **Long-polling**: reduces empty receive costs and latency (configured at 20s wait)
- **Visibility timeout**: allows a consumer to "hold" a message while processing it, preventing other instances from picking it up in parallel

### Trade-offs

| Advantage | Disadvantage |
|-----------|-------------|
| Fully managed — no ops burden | Not a streaming platform (cannot replay arbitrary history like Kafka) |
| Scales to millions of messages/day | No native message ordering (unless using FIFO queues) |
| Integrates with LocalStack for local dev | Higher cost at very high throughput compared to Kafka |
| Built-in retry + DLQ | Max message size: 256 KB |

### Why Not Kafka?
Kafka is a better fit when you need **event streaming with replay** (e.g., rebuilding read models, analytics). SQS is the right choice when you need **reliable task queue semantics** (process each event once, retry on failure) — which is exactly the gamification use case.

---

## 2. Why MongoDB in GameService?

### Decision
Use MongoDB as the event store for `GameEventDocument` — the raw audit log of all transaction events received by GameService.

### Rationale
- **Schema flexibility**: transaction events may evolve (new fields) without requiring database migrations
- **Append-only log**: documents are inserted (never updated), which aligns naturally with MongoDB's document model
- **Idempotency layer**: MongoDB acts as the first idempotency check at the messaging layer (`findById(event.eventId())`) before any JPA writes occur
- **High write throughput**: MongoDB handles high-frequency inserts well without lock contention

### Trade-offs

| Advantage | Disadvantage |
|-----------|-------------|
| No migration scripts for event schema changes | Two databases in one service (MongoDB + H2) adds operational complexity |
| Natural fit for append-only event logs | Transactions between MongoDB and H2 cannot be atomic (two separate stores) |
| `_id` uniqueness enforces idempotency at the storage layer | More infrastructure to run locally (requires `docker-compose`) |

---

## 3. Why Redis in LoginService?

### Decision
Use Redis for two distinct concerns in LoginService: **session storage** and **distributed rate limiting**.

### Session Storage
After login, a `SessionDTO` object (sessionId, username, contractService flag, symmetricKey, role) is serialized and stored in Redis with an expiry. When TransactionService calls `GET /api/v1/me`, LoginService retrieves the session from Redis using the session ID embedded in the JWT.

**Why Redis over a relational database?**
- Sub-millisecond reads — session lookups are on the hot path (every transaction triggers one)
- Automatic expiry (TTL) — no cleanup jobs needed
- Horizontally shareable — all LoginService instances share the same Redis instance

### Rate Limiting
A Lua script atomically increments a counter per client IP and sets a TTL on first use. If the counter exceeds `max-requests` (default: 5) within `window-seconds` (default: 60), the request is rejected with HTTP 429.

**Why Lua in Redis?**  
The INCR + EXPIRE operation must be atomic to avoid race conditions when multiple instances handle concurrent requests. Redis executes Lua scripts as a single atomic transaction — no two operations can interleave.

### Trade-offs

| Advantage | Disadvantage |
|-----------|-------------|
| Microsecond session reads | Additional infrastructure dependency |
| TTL-based session expiry is automatic | Redis unavailability would affect session lookups |
| Atomic Lua rate limiting works across multiple instances | Rate limiter **fails open** on Redis unavailability (by design — availability > security) |

---

## 4. Why Three Separate Microservices?

### Decision
Split the system into LoginService, TransactionService, and GameService — each with its own database, Docker image, and deployment lifecycle.

### Rationale

| Principle | How It Applies |
|-----------|----------------|
| **Single Responsibility** | Each service has one clear bounded context: auth, transactions, gamification |
| **Independent deployment** | GameService can be updated (e.g., new mission types) without touching LoginService or TransactionService |
| **Independent scaling** | If gamification processing lags, only GameService needs more instances |
| **Fault isolation** | GameService crashing does not affect transaction acceptance |
| **Technology fit** | Each service can choose the right database for its domain |

### Trade-offs

| Advantage | Disadvantage |
|-----------|-------------|
| Each service is small and understandable in isolation | More infrastructure (3 JVMs, 3 build pipelines, 3 docker images) |
| Independent deployability | Distributed tracing and debugging across services is harder |
| No blast radius on failure | Network calls introduce latency and failure modes not present in a monolith |
| Teams can own services independently | Data consistency across services requires eventual consistency patterns |

---

## 5. Why Asynchronous Architecture for Gamification?

### Decision
TransactionService publishes an event to SQS and responds `202 Accepted` immediately. Gamification processing happens asynchronously in GameService.

### Rationale
Gamification is not time-sensitive from the customer's perspective — the customer does not need to see their points updated before the `POST /transactions` response returns. However, the transaction confirmation **must be fast and reliable**.

If gamification were synchronous:
- A GameService slowdown (e.g., H2 lock, MongoDB write issue) would directly delay transaction confirmations
- A GameService crash would cause transaction failures — unacceptable for a banking product
- Scaling GameService independently would be impossible (it would be tightly coupled to the transaction path)

### Trade-offs

| Advantage | Disadvantage |
|-----------|-------------|
| Transaction latency unaffected by gamification | Points are not immediately visible after a transaction (eventual consistency) |
| GameService failures do not fail transactions | Debugging requires correlating event IDs across services |
| Retry semantics built into SQS | SQS adds infrastructure cost and complexity |
| GameService can be scaled or replaced independently | Race conditions must be explicitly handled (optimistic locking, idempotency) |

---

## 6. Why Optimistic Locking on CustomerProgress?

### Decision
The `CustomerProgress` entity uses JPA `@Version` for optimistic locking.

### Rationale
When multiple SQS messages for the same customer arrive concurrently (e.g., during a burst), multiple GameService threads could attempt to update the same `CustomerProgress` row simultaneously. Without locking, **lost updates** could occur — one thread's point increment could overwrite another's.

Optimistic locking detects concurrent modifications at commit time (via a version column check) and throws an `OptimisticLockException`, which triggers a retry. This is preferred over pessimistic locking because:
- Contention per customer is expected to be low
- No database-level row locks are held during processing (better throughput)

---

## 7. Why Java 21 Virtual Threads?

### Decision
Both LoginService and GameService configure Java 21 virtual threads (`threads.virtual.enabled: true`). GameService uses a `virtualThreadExecutor` for concurrent SQS message processing.

### Rationale
Virtual threads allow high I/O concurrency with minimal thread overhead. In GameService, each SQS message is dispatched to a virtual thread for independent processing. Because virtual threads are lightweight (few KB vs ~1MB for platform threads), thousands of concurrent messages can be in-flight without exhausting OS thread pool limits.

For LoginService, virtual threads improve throughput under concurrent login requests without requiring reactive programming.

---

## 8. Why Resilience4j in TransactionService?

### Decision
Calls from TransactionService to LoginService's `/me` endpoint are protected by a **circuit breaker** and **retry** configuration via Resilience4j.

### Rationale
The `/me` call is synchronous and on the critical path. If LoginService becomes slow or unavailable:
- Without a circuit breaker, threads would pile up waiting for the timeout, eventually exhausting the connection pool
- With a circuit breaker, after 50% failures in a 10-call sliding window, the circuit opens and requests fail-fast for 30s before trying again

Retry with exponential backoff handles transient network blips (e.g., a single dropped packet) without requiring a circuit open.
