# End-to-End Flow

This document traces the complete request flow from customer login to gamification processing, explaining the role and rationale of each step.

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

**What happens:** The mobile app sends credentials to `POST /api/v1/login`.

**Why it works this way:** LoginService is the single gatekeeper for authentication. Centralizing credential handling means that TransactionService and GameService never touch passwords — they only trust signed tokens.

LoginService:
1. **Rate limit check** (Redis Lua INCR): enforces max 5 attempts per IP per 60s — protecting against brute-force attacks
2. **Authentication** (Spring Security `AuthenticationManager`): verifies credentials against H2/PostgreSQL
3. **Session creation**: generates a `sessionId` (UUID) and stores `SessionDTO` in Redis with a 5-minute TTL — Redis is used here because session lookups are on the hot path of every transaction and must be sub-millisecond
4. **JWT issuance**: signs a token embedding `customerId`, `channel`, `sessionId`, and `role` — the JWT carries just enough identity information for downstream validation without requiring additional roundtrips
5. Returns `{ "token": "eyJ..." }`

---

### Step 2 — JWT Token in the Client

**What happens:** The client stores the JWT and includes it in the `Authorization: Bearer <token>` header for all subsequent requests.

**Why it works this way:** The JWT is a self-describing credential. TransactionService can extract the `customerId` and `channel` from it without calling LoginService — reducing latency and avoiding a dependency for identity parsing. The session validation call (`/me`) is reserved only for confirming the session is still active.

---

### Step 3 — Transaction Request

**What happens:** The mobile app sends `POST /transactions` to TransactionService.

**Why it works this way:** TransactionService is the only entry point for transactions. The `X-Idempotency-Key` header allows clients to safely retry failed requests without double-processing — the key is forwarded as the `eventId` in the SQS event, enabling end-to-end deduplication.

- Request body: `{ "type": "PIX", "amount": 500.00 }`
- `Authorization: Bearer <jwt>`
- Optional: `X-Idempotency-Key: <uuid>` (used as `eventId` if provided)

---

### Step 4 — JWT Validation in TransactionService

**What happens:** TransactionService's `JwtDetails` security filter validates the JWT signature and populates the security context.

**Why it works this way:** JWT validation is done locally (no network call) by verifying the HS256 signature against the shared secret. This ensures that every request is authenticated without adding network latency — the token carries enough information (`customerId`, `channel`, `sessionId`) to proceed without asking LoginService.

1. Extracts and validates the JWT signature (HS256)
2. Reads `customerId`, `channel`, and raw token
3. Sets the Spring Security `Authentication` context

---

### Step 5 — Session Validation via GET /me

**What happens:** TransactionService calls `LoginService GET /api/v1/me` with the Bearer token.

**Why this synchronous call is justified:** JWT validation only proves the token was validly issued. It does not confirm that the session is still active (the user may have logged out). The `/me` call validates that the session exists in Redis and that the customer has `contractService == true` (has subscribed to the gamification service). This is a security requirement — without it, a customer with an expired or revoked session could still transact.

This is the **only synchronous inter-service call** in the architecture. It is justified because session validity is a hard prerequisite — unlike gamification, it cannot be deferred.

This call is protected by:
- **Resilience4j retry** (3 attempts, exponential backoff, 500ms base) — handles transient network blips
- **Resilience4j circuit breaker** (opens after 50% failures in 10-call window, stays open 30s) — prevents LoginService degradation from cascading into TransactionService

---

### Step 6 — Business Validation

**What happens:** TransactionService enforces business rules on the transaction request.

**Why it works this way:** Business validation belongs in TransactionService, not in GameService. Rejecting invalid transactions before they reach the queue prevents GameService from having to handle invalid events and ensures the queue contains only well-formed, authorized events.

- `channel` must be `"MOBILE"` — only mobile app transactions are eligible for gamification
- Transaction type and amount pass `TransactionValidator` checks

---

### Step 7 — SQS Event Publication

**What happens:** A `TransactionEvent` is serialized as JSON and published to the SQS `transactions` queue.

**Why publish to a queue instead of calling GameService directly:** At this point, TransactionService has completed its responsibility — it has validated and accepted the transaction. Gamification is a downstream concern that does not affect the transaction outcome. Publishing to SQS decouples the two concerns:

- GameService can be unavailable, scaled, or restarted without affecting this step
- SQS persists the event durably until GameService is ready to process it
- The event can be replayed from the DLQ if processing fails

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

**What happens:** TransactionService immediately returns `HTTP 202 Accepted`.

**Why 202 and not 200:** `202 Accepted` semantically means "the request has been accepted for processing, but the processing has not been completed." This is architecturally honest — the transaction is accepted and the event is enqueued, but gamification will happen asynchronously. The customer's app receives a fast, definitive confirmation without waiting for downstream processing.

---

### Steps 9–23 — Asynchronous Gamification Processing

**What happens:** GameService independently processes the event from SQS.

**Why this is decoupled from the previous steps:** GameService has no knowledge of when or how TransactionService published the event. It polls SQS continuously and processes messages at its own pace. This independence is the core architectural property that makes the system resilient.

GameService runs a continuous SQS polling loop (virtual thread background process):

1. **Poll SQS** — long-polling (up to 20s wait) reduces empty receives and cost; up to 10 messages per batch
2. **Dispatch to virtual threads** — each message is processed in its own virtual thread for isolated, concurrent processing
3. **MongoDB idempotency check** — look up `GameEventDocument` by `eventId`; if found, the message was already processed — acknowledge and skip. This is the first line of defence against SQS at-least-once redelivery
4. **Store `GameEventDocument` in MongoDB** — immutable audit record; its `_id` uniqueness also enforces the idempotency check atomically
5. **Filter: PIX only** — only PIX transactions are eligible for gamification; other types are acknowledged and discarded
6. **Monthly reset check** — if `CustomerProgress.lastReset` month differs from the current month, reset `totalPoints` to 0 and `level` to 1. This ensures engagement is rewarded continuously rather than one-time
7. **Benefit redemption check** — if the customer has already redeemed a benefit this month, skip further gamification processing to avoid double-rewards
8. **H2 idempotency check** — look up `ProcessedEvent` by `eventId`; catches the narrow race where MongoDB was written but H2 was not yet committed before a previous crash
9. **Mission evaluation** — for each active `Mission` in H2, check if the transaction `amount` falls within `[minValue, maxValue]` and if the customer has not already completed this mission; award points and record a `MissionCompletion` for each eligible mission
10. **Level recalculation** — find the highest `LevelRule` where `minPoints <= totalPoints`; update `CustomerProgress.level`
11. **Save `CustomerProgress`** — uses optimistic locking (`@Version`) to detect concurrent updates and retry if needed
12. **Save `ProcessedEvent`** — marks this event as fully processed in H2 (second idempotency layer)
13. **Delete SQS message** — explicit acknowledgement after all writes succeed; if this step is not reached (e.g., application crash), SQS will redeliver the message and the idempotency layers will handle it

**On processing failure:**
- Extend SQS visibility timeout exponentially (`60s × receiveCount`, max 12h)
- After 5 failed attempts (`max-receive-count`), the message is automatically forwarded to the DLQ

---

## Idempotency Guarantee

The system uses **dual idempotency** to handle SQS at-least-once delivery.

| Layer | Mechanism | Database | Covers |
|-------|-----------|----------|--------|
| **Message layer** | `GameEventDocument` lookup by `eventId` before any processing | MongoDB | Redeliveries before any state is written |
| **Business logic layer** | `ProcessedEvent` record checked before applying rules | H2 (JPA) | Redeliveries after MongoDB write but before H2 commit |

Together, these two layers guarantee **exactly-once rule application** under all failure scenarios, including mid-processing crashes. This is necessary because SQS provides at-least-once semantics — the architecture explicitly takes responsibility for the "exactly-once" guarantee at the application level.

---

## Flow Summary

| Phase | Service | Key Action | Architectural Property |
|-------|---------|------------|------------------------|
| Authentication | LoginService | Issue JWT + store session | Single auth gateway |
| Transaction acceptance | TransactionService | Validate + publish event | Critical path isolation |
| Session validation | LoginService | Confirm active session | Security without stored state in TS |
| Event buffering | SQS | Persist event durably | Decoupling + retry |
| Gamification | GameService | Process rules asynchronously | Fault isolation + independent scaling |
