# TransactionService

## Responsibility

TransactionService is the **transaction acceptance gateway** for the Itaú microservices platform. It is the entry point for all financial transaction requests from customer-facing applications.

Its responsibilities are:
- Accept and validate PIX transaction requests from authenticated MOBILE-channel customers
- Verify the customer's active session with LoginService
- Publish validated transaction events to AWS SQS for downstream processing
- Return an immediate `202 Accepted` response to the caller
- Track metrics for observability (Prometheus / Micrometer)

TransactionService is **intentionally unaware of gamification logic**. Its only concern is transaction acceptance — what happens downstream is delegated asynchronously.

### Why This Boundary?

TransactionService exists as a separate service because **transaction acceptance is the revenue-critical operation** that must be available, fast, and observable independently of everything else.

Key design decisions that follow from this:
- **No knowledge of gamification**: TransactionService publishes an event and moves on. Adding gamification awareness would couple it to the GameService deployment lifecycle and failure modes.
- **202 Accepted semantics**: The service accepts the transaction (validates it, enqueues it) but does not wait for downstream processing. This is architecturally honest and keeps the critical path short.
- **Resilience4j protection on LoginService calls**: The only synchronous dependency is session validation — which is a security requirement, not a business feature. It is protected with a circuit breaker to prevent LoginService degradation from cascading into this service.

---

## Technologies

| Technology             | Role                                                       |
|------------------------|------------------------------------------------------------|
| Spring Boot            | Application framework                                      |
| Spring Security        | JWT-based stateless authentication filter                  |
| Spring WebFlux (WebClient) | Non-blocking HTTP client for LoginService calls       |
| AWS SDK v2 (SQS)       | Event publication to SQS                                   |
| Resilience4j           | Circuit breaker + retry on LoginService calls              |
| Micrometer + Prometheus | Metrics (transaction counts, durations, circuit breaker health) |
| WireMock               | LoginService mock for integration testing                  |
| LocalStack             | Local SQS simulation for development                       |
| Java 21                | Runtime                                                    |
| Lombok                 | Boilerplate reduction                                      |

---

## API Endpoints

| Method | Path            | Description                                | Auth   |
|--------|-----------------|--------------------------------------------|--------|
| `POST` | `/transactions` | Accept and enqueue a transaction event     | Bearer |

### POST /transactions

**Request headers:**
```
Authorization: Bearer <jwt>
X-Idempotency-Key: <optional-uuid>
```

**Request body:**
```json
{
  "type": "PIX",
  "amount": 500.00
}
```

**Response (202 Accepted):**
```json
{
  "transactionId": "550e8400-...",
  "customerId": "customer123",
  "type": "PIX",
  "amount": 500.00,
  "status": "ACCEPTED",
  "timestamp": "2026-04-26T22:00:00Z"
}
```

**Error responses:**

| HTTP Status | Condition |
|-------------|-----------|
| `401 Unauthorized` | Missing or invalid JWT |
| `400 Bad Request` | Invalid request body (missing fields, bad type) |
| `403 Forbidden` | Channel is not MOBILE or contractService is false |
| `503 Service Unavailable` | LoginService unreachable (circuit open) |

---

## Main Classes

| Class | Package | Role |
|-------|---------|------|
| `TransactionServiceApplication` | root | Spring Boot entry point |
| `TransactionController` | `controller` | REST endpoint for `POST /transactions` |
| `GlobalExceptionHandler` | `controller` | Maps domain exceptions to HTTP status codes |
| `TransactionServiceImpl` | `service` | Main business logic: validates, calls `/me`, publishes to SQS |
| `TransactionValidator` | `service` | Validates transaction request fields |
| `TransactionService` (interface) | `service` | Contract for transaction processing |
| `LoginClient` | `infrastructure.client` | WebClient-based HTTP client for LoginService |
| `SqsProducer` | `infrastructure.sqs` | Publishes `TransactionEvent` JSON to SQS |
| `SqsConfig` | `infrastructure.sqs` | AWS SQS client configuration |
| `LoginClientAdapter` | `adapters` | Implements `LoginClientDomain` using `LoginClient` |
| `SqsProducerAdapter` | `adapters` | Implements `SqsProducerDomain` using `SqsProducer` |
| `JwtDetails` | `infrastructure.security` | Holds raw JWT token and channel for the security context |
| `TransactionEvent` | `model.event` | Immutable event record published to SQS |
| `TransactionRequest` | `model.request` | Incoming request DTO |
| `TransactionResponse` | `model.response` | Outgoing response DTO |
| `SessionDTO` | `model.session` | Session data returned by LoginService `/me` |

---

## Internal Flow

```
POST /transactions
      │
      ├── Spring Security filter chain
      │   └── JwtAuthenticationFilter
      │       ├── extract Bearer token from Authorization header
      │       ├── validate JWT signature (HS256, shared secret with LoginService)
      │       ├── extract customerId (sub) + channel + raw token
      │       └── populate SecurityContextHolder with Authentication
      │
      └── TransactionController.createTransaction()
          │
          └── TransactionServiceImpl.processTransaction(request, idempotencyKey)
              │
              ├── Read Authentication from SecurityContext
              │   └── customerId, channel, rawToken
              │
              ├── TransactionValidator.validate(request)
              │   └── validates type, amount not null/negative
              │
              ├── validateChannel(channel)
              │   └── reject if channel != "MOBILE"
              │
              ├── loginClient.getSession(rawToken)  ← protected by Resilience4j
              │   └── GET http://login-service/api/v1/me
              │       Authorization: Bearer <rawToken>
              │   → SessionDTO { sessionId, username, contractService, role }
              │
              ├── validate contractService == true
              │   └── throw BusinessException if false
              │
              ├── build TransactionEvent
              │   └── { eventId, customerId, type, amount, channel, timestamp }
              │       eventId = X-Idempotency-Key header (if provided) or new UUID
              │
              ├── sqsProducer.publish(event)
              │   └── serialize to JSON, send to SQS queue URL
              │
              ├── increment Micrometer counter (transactions.total)
              │
              └── return TransactionResponse { 202 ACCEPTED }
```

---

## Resilience4j Configuration

### Why Resilience4j Here (and Not in Other Services)?

The `/me` call to LoginService is the **only synchronous network dependency** in this service, and it is on the critical path of every transaction. Without resilience patterns:

- A slow LoginService delays every transaction response (threads pile up waiting)
- A down LoginService causes 100% transaction failure until recovered
- A thread pool exhaustion in this service caused by LoginService degradation would make this service appear to be the faulty one (cascading failure)

Resilience4j's circuit breaker detects degradation and fails-fast, preserving this service's resources for requests that can succeed. Retry absorbs transient blips without surfacing them to the client.

### Circuit Breaker — `loginService`

| Property | Value | Meaning |
|----------|-------|---------|
| `sliding-window-size` | 10 | Evaluate last 10 calls |
| `failure-rate-threshold` | 50% | Open circuit after 50% failures |
| `wait-duration-in-open-state` | 30s | Stay open for 30s before trying again |
| `permitted-number-of-calls-in-half-open-state` | 3 | Allow 3 test calls before deciding to close |

### Retry — `loginService`

| Property | Value |
|----------|-------|
| `max-attempts` | 3 |
| `wait-duration` | 500ms |
| `exponential-backoff-multiplier` | 2 |
| Retry on | `IOException`, `TimeoutException`, `WebClientRequestException` |
| Do NOT retry | `BusinessException`, `UnauthorizedException` |

**Why not retry on business exceptions?** Retrying a `BusinessException` (e.g., `contractService == false`) or `UnauthorizedException` would not change the outcome — these are deterministic rejections, not transient failures. Retrying them wastes time and may confuse monitoring.

---

## Idempotency

TransactionService supports the `X-Idempotency-Key` request header. If provided, the header value is used as the `eventId` of the SQS event. This allows:

- Clients to safely retry a failed request (e.g., a mobile app timeout) without double-processing
- GameService to deduplicate by `eventId` regardless of how many times the message is redelivered

If the header is absent, a new `UUID` is generated per request.

**End-to-end idempotency chain**: the client's idempotency key flows through TransactionService → SQS event → GameService deduplication. This ensures the same business transaction is processed exactly once even if the client, TransactionService, or SQS redelivers the same operation.

---

## Metrics

TransactionService exposes Prometheus metrics via `/actuator/prometheus`:

| Metric | Type | Description |
|--------|------|-------------|
| `transactions.total` | Counter | Total accepted transactions |
| `transactions.failures` | Counter | Total rejected/failed transactions |
| `login.service.request.duration` | Timer | Duration of `/me` calls to LoginService |

**Why metrics here?** TransactionService is the revenue-critical entry point. Monitoring transaction throughput and failure rates is the primary health signal for the system. The `/me` call duration metric is specifically important for detecting LoginService degradation before it triggers the circuit breaker.

---

## Integrations

| Integration | Direction | Protocol | Notes |
|-------------|-----------|----------|-------|
| LoginService (`/me`) | Outbound | HTTP REST (WebClient) | Session validation on every transaction — protected by circuit breaker + retry |
| AWS SQS | Outbound | AWS SDK v2 | Publish `TransactionEvent` JSON — fire-and-forget after validation |

---

## Configuration

Key configuration properties (`application.yml`):

```yaml
server:
  port: 8080

jwt:
  secret: ${JWT_SECRET}

login-service:
  base-url: ${LOGIN_SERVICE_URL:http://localhost:8081}
  timeout-millis: ${LOGIN_SERVICE_TIMEOUT_MILLIS:2000}

aws:
  sqs:
    queue-url: ${SQS_QUEUE_URL:http://localhost:4566/000000000000/transactions}
    region: ${AWS_REGION:us-east-1}
    endpoint: ${AWS_ENDPOINT:http://localhost:4566}
```
