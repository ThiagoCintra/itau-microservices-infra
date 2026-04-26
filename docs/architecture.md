# Architecture — Itaú Microservices Platform

## Overview

The platform is composed of **three independently deployable microservices** that communicate through a combination of synchronous REST calls and asynchronous messaging via AWS SQS.

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

The core design decision is to **decouple the transaction processing path from the gamification logic** using an asynchronous event-driven approach.

### The Three Services

| Service              | Responsibility                                | Technology    | Port  |
|----------------------|-----------------------------------------------|---------------|-------|
| **LoginService**     | Authentication, JWT issuance, session storage | Spring Boot + Redis + H2 | 8081 |
| **TransactionService** | Transaction acceptance, JWT validation, SQS publishing | Spring Boot + SQS | 8080 |
| **GameService**      | Gamification processing (missions, levels, benefits) | Spring Boot + SQS + MongoDB + H2 | 8082 |

---

## Why NOT Synchronous Calls for Gamification?

A naive implementation would have `TransactionService` call `GameService` directly after receiving a transaction:

```
Client → TransactionService → GameService (sync) → response
```

This approach has severe problems in a banking context:

| Problem | Impact |
|---------|--------|
| **Latency coupling** | If GameService is slow (e.g., DB contention), every transaction becomes slow |
| **Availability coupling** | If GameService is down, transactions fail — gamification outage = transaction outage |
| **Tight deployment coupling** | You cannot deploy/restart GameService without risking transaction errors |
| **No retry semantics** | On a sync failure, the transaction must either fail or gamification is silently skipped |
| **No backpressure control** | A burst of transactions would overwhelm GameService with no way to buffer |

### The Asynchronous Solution

By publishing events to SQS and having GameService consume them independently:

- Transactions are **accepted immediately** — P99 latency stays low
- Gamification **failures are isolated** — transactions always succeed
- GameService can be **scaled, restarted, or upgraded** without affecting transactions
- **Natural buffering**: SQS holds events during GameService downtime and replays them
- **Retry semantics**: failed messages are retried with exponential backoff and routed to a DLQ

---

## Messaging: AWS SQS

The messaging layer uses **Amazon Simple Queue Service (SQS)** via LocalStack in local/test environments.

### Queue Configuration

| Queue              | Purpose                                                 |
|--------------------|---------------------------------------------------------|
| `transactions`     | Main queue — events produced by TransactionService      |
| `transactions-dlq` | Dead Letter Queue — events that failed after max retries |

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
                            (or increase visibility timeout on failure)
```

### Retry / DLQ Strategy

- Each message tracks an `ApproximateReceiveCount`
- On processing failure, the **visibility timeout is extended** exponentially (60s × receiveCount, max 12h)
- After exceeding `max-receive-count` (default 5), the message is **forwarded to the DLQ** and deleted from the main queue

---

## Service Separation

### LoginService

- Owns **authentication** and **session management**
- Issues **JWT tokens** that carry: `customerId`, `channel`, `sessionId`, `role`
- Maintains **active sessions in Redis** for fast lookup by `TransactionService`
- Enforces **rate limiting** (5 login attempts per 60s per IP) via a Redis-backed Lua atomic counter
- Does **not** know about transactions or gamification

### TransactionService

- Owns the **transaction acceptance** business logic
- Validates: JWT signature, MOBILE channel, contracted service, amount limits
- Calls `LoginService /me` to validate the session (synchronous — session validation is on the critical path)
- Protected by **Resilience4j circuit breaker + retry** on LoginService calls
- Once validated, **publishes an event to SQS** and responds `202 Accepted` immediately
- Does **not** process gamification or know about game rules

### GameService

- Owns **all gamification logic**
- Runs a **background SQS polling loop** (virtual threads, long-polling)
- Is completely **stateless from the HTTP perspective** — no REST endpoints for transaction ingestion
- Uses **H2 (JPA)** for relational game state (customer progress, missions, level rules, benefit redemptions)
- Uses **MongoDB** as an event store for audit and idempotency at the messaging layer

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

## Low Coupling

Each service:
- Has its **own database** (no shared schema)
- Communicates only through **defined interfaces** (JWT token, REST `/me`, SQS events)
- Can be **deployed, scaled, and restarted independently**
- Has its **own Docker image** and `docker-compose.yml`

---

## Infrastructure Components

| Component     | Role                                     | Local Replacement |
|---------------|------------------------------------------|-------------------|
| AWS SQS       | Message queue between TS and GS          | LocalStack        |
| Redis         | Session storage + rate limiting for LS   | Docker Redis      |
| H2 Database   | Relational store for LS and GS (local)   | In-memory         |
| MongoDB       | Event store / audit log for GS           | Docker MongoDB    |
| WireMock      | Mock for LoginService in TS integration tests | Docker WireMock |
