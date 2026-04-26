# End-to-End Flow

This document describes the complete request flow from customer login to gamification processing.

---

## Flow Diagram

```
Customer (Mobile App)
      │
      │  1. POST /api/v1/login  { username, password }
      ▼
┌─────────────────────────────────────────────┐
│              LoginService (:8081)            │
│  ├── Authenticate user (Spring Security)    │
│  ├── Check rate limit via Redis (Lua INCR)  │
│  ├── Load user from H2 database             │
│  ├── Store SessionDTO in Redis (TTL 5min)   │
│  └── Issue JWT token                        │
└──────────────────────────────────────────────┘
      │
      │  2. Response: { "token": "eyJhbGciOiJIUzI1NiJ9..." }
      ▼
Customer stores JWT token
      │
      │  3. POST /transactions  { type: "PIX", amount: 500.00 }
      │     Authorization: Bearer <jwt>
      │     X-Idempotency-Key: <optional-uuid>
      ▼
┌─────────────────────────────────────────────┐
│           TransactionService (:8080)         │
│  ├── Extract JWT from Authorization header  │
│  ├── Validate JWT signature (HS256)         │
│  ├── Extract customerId + channel from JWT  │
│  ├── Validate channel == "MOBILE"           │
│  ├── Validate transaction (amount, type)    │
│  │                                          │
│  │  4. GET /api/v1/me (sync call)           │
│  │     Authorization: Bearer <jwt>          │
│  │     (protected by circuit breaker+retry) │
│  ▼                                          │
│ ┌──────────────────────────────────────┐    │
│ │         LoginService (:8081) /me     │    │
│ │  ├── Validate JWT                   │    │
│ │  ├── Load SessionDTO from Redis     │    │
│ │  └── Return { sessionId, username,  │    │
│ │              contractService, role }│    │
│ └──────────────────────────────────────┘    │
│                                             │
│  5. Validate contractService == true        │
│  6. Build TransactionEvent (JSON)           │
│     { eventId, customerId, type, amount,    │
│       channel, timestamp }                  │
│  7. Publish event to SQS `transactions`     │
└──────────────────────────────────────────────┘
      │
      │  8. Response: 202 Accepted
      │     { transactionId, customerId, type,
      │       amount, status:"ACCEPTED", timestamp }
      ▼
Customer receives confirmation immediately
      │
      │  (asynchronously — decoupled from above)
      ▼
┌──────────────────────────────────────────────────┐
│         AWS SQS queue: `transactions`             │
│  (LocalStack in local environment)                │
└────────────────────────────┬─────────────────────┘
                             │
                             │  9. SQS Consumer polls (long-polling, max 20s wait)
                             ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                         GameService (:8082)                                   │
│                                                                               │
│  10. Receive message from SQS                                                 │
│  11. Parse TransactionEvent JSON                                              │
│  12. Idempotency check:                                                       │
│      └── gameEventRepository.findById(eventId)  [MongoDB]                    │
│          → if found: delete message, skip processing                         │
│                                                                               │
│  13. Save GameEventDocument to MongoDB  [audit log]                          │
│                                                                               │
│  14. Filter: type != "PIX" → skip                                            │
│                                                                               │
│  15. Secondary idempotency check:                                             │
│      └── processedEventRepository.existsByEventId()  [H2]                   │
│          → if found: skip                                                    │
│                                                                               │
│  16. Load or create CustomerProgress  [H2 with @Version optimistic lock]     │
│                                                                               │
│  17. Monthly reset check:                                                    │
│      └── if lastReset.month != now.month:                                    │
│          → totalPoints = 0, level = 1, lastReset = now                       │
│                                                                               │
│  18. Benefit redemption check:                                               │
│      └── if customer redeemed benefit this month → skip further processing  │
│                                                                               │
│  19. Mission evaluation:                                                     │
│      └── for each active Mission in H2:                                      │
│          ├── amount in [minValue, maxValue]?                                  │
│          ├── mission already completed by this customer?  [MissionCompletion] │
│          └── if eligible: award points, save MissionCompletion               │
│                                                                               │
│  20. Level recalculation:                                                    │
│      └── select highest LevelRule where minPoints <= totalPoints             │
│          → update CustomerProgress.level                                     │
│                                                                               │
│  21. Save CustomerProgress  [H2]                                             │
│                                                                               │
│  22. Save ProcessedEvent  [H2]  (idempotency marker)                        │
│                                                                               │
│  23. Delete SQS message (acknowledge)                                        │
│                                                                               │
│  On error:                                                                   │
│  └── increase SQS visibility timeout (exponential backoff)                  │
│      → after max-receive-count (5): forward to DLQ, delete from main queue  │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Step-by-Step Description

### Step 1 — Customer Login

The mobile app sends credentials to `POST /api/v1/login`.

LoginService:
1. Checks the Redis rate limiter (max 5 attempts per IP per 60s)
2. Authenticates with Spring Security (`AuthenticationManager`)
3. Loads the `UserAccount` from H2 (or PostgreSQL in production)
4. Generates a `sessionId` (random UUID) and a `symmetricKey`
5. Creates a `SessionDTO` and stores it in Redis with a 5-minute TTL
6. Issues a JWT token containing `customerId` (username), `channel`, `sessionId`, and `role`
7. Returns `{ "token": "eyJ..." }`

---

### Step 2 — JWT Token in the Client

The client stores the JWT token and includes it in the `Authorization: Bearer <token>` header for all subsequent requests.

---

### Step 3 — Transaction Request

The mobile app sends `POST /transactions` to TransactionService with:
- Request body: `{ "type": "PIX", "amount": 500.00 }`
- `Authorization: Bearer <jwt>`
- Optional: `X-Idempotency-Key: <uuid>` (used as `eventId` if provided)

---

### Step 4 — JWT Validation in TransactionService

TransactionService's `JwtDetails` security filter:
1. Extracts and validates the JWT signature
2. Reads `customerId`, `channel`, and raw token
3. Sets the Spring Security `Authentication` context

---

### Step 5 — Session Validation (GET /me)

TransactionService calls `LoginService GET /api/v1/me` with the Bearer token to confirm:
- The session is still active in Redis
- The customer has `contractService == true` (has subscribed to the service)

This call is protected by:
- **Resilience4j retry** (3 attempts, exponential backoff, 500ms base)
- **Resilience4j circuit breaker** (opens after 50% failures in 10-call window, stays open 30s)

---

### Step 6 — Business Validation

TransactionService validates:
- `channel` must be `"MOBILE"` — other channels (branch, ATM, internet banking) are rejected
- Transaction type and amount pass `TransactionValidator` checks

---

### Step 7 — SQS Event Publication

A `TransactionEvent` is serialized as JSON and sent to SQS:

```json
{
  "eventId": "d4f2a1c3-...",
  "customerId": "customer123",
  "type": "PIX",
  "amount": 500.00,
  "channel": "MOBILE",
  "timestamp": "2026-04-26T22:00:00Z"
}
```

---

### Step 8 — 202 Accepted Response

TransactionService immediately returns `HTTP 202 Accepted` with a `TransactionResponse`. The customer's app receives a confirmation without waiting for gamification to complete.

---

### Steps 9–23 — Asynchronous Gamification Processing

GameService runs a continuous SQS polling loop (in a virtual thread background process):

1. **Poll** SQS for up to 10 messages, waiting up to 20 seconds for messages
2. **Dispatch** each message to a virtual thread for independent processing
3. **Idempotency** at the MongoDB level (check `GameEventDocument._id`)
4. **Store** the event document in MongoDB as an immutable audit record
5. **Filter** — only PIX events are processed
6. **Monthly reset** — automatically reset points if the month changed
7. **Benefit check** — skip additional processing if benefit already redeemed this month
8. **Mission evaluation** — check each active mission, award points for newly eligible ones
9. **Level update** — recalculate level from accumulated points using stored level rules
10. **Persist** progress and mark event as processed
11. **Delete** the SQS message to acknowledge successful processing

---

## Idempotency Guarantee

The system uses **dual idempotency** to handle SQS at-least-once delivery:

| Layer | Mechanism | Database |
|-------|-----------|----------|
| **SQS message layer** | `GameEventDocument` lookup by `eventId` before any processing | MongoDB |
| **Business logic layer** | `ProcessedEvent` record checked before applying rules | H2 (JPA) |

This ensures that even if the same SQS message is delivered multiple times (e.g., after a crash between processing and message deletion), the gamification rules are applied exactly once.
