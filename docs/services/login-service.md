# LoginService

## Responsibility

LoginService is the **authentication and session management gateway** for the Itaú microservices platform. It is the only service that knows about user credentials and issues JWT tokens that other services trust for authorization.

Its responsibilities are:
- Authenticate customers with username/password
- Issue signed JWT tokens
- Store and serve active sessions from Redis
- Rate-limit login attempts per IP address
- Expose a `/me` endpoint for other services to validate active sessions

---

## Technologies

| Technology            | Role                                                  |
|-----------------------|-------------------------------------------------------|
| Spring Boot 4.0.5     | Application framework                                 |
| Spring Security       | Authentication pipeline, JWT filter chain             |
| Spring Data JPA       | User persistence (H2 in-memory / PostgreSQL in prod)  |
| Spring Data Redis     | Session storage and rate limiting                     |
| JJWT 0.11.5           | JWT generation and parsing                            |
| H2 Database           | In-memory relational store for users (local/test)     |
| Java 21 Virtual Threads | High-concurrency request handling                  |
| Lombok + MapStruct    | Boilerplate reduction and DTO mapping                 |

---

## API Endpoints

| Method | Path              | Description                          | Auth     |
|--------|-------------------|--------------------------------------|----------|
| `POST` | `/api/v1/login`   | Authenticate user and issue JWT      | None     |
| `GET`  | `/api/v1/me`      | Return active session for a JWT      | Bearer   |

### POST /api/v1/login

**Request body:**
```json
{
  "username": "customer123",
  "password": "secret"
}
```

**Response (200 OK):**
```json
{
  "token": "eyJhbGciOiJIUzI1NiJ9..."
}
```

**Response (429 Too Many Requests):** Rate limit exceeded (5 attempts / 60s per IP)

### GET /api/v1/me

Used by TransactionService to validate an active session.

**Request header:** `Authorization: Bearer <token>`

**Response (200 OK):**
```json
{
  "sessionId": "abc-123",
  "username": "customer123",
  "contractService": true,
  "role": "CUSTOMER"
}
```

---

## Main Classes

| Class | Package | Role |
|-------|---------|------|
| `LoginApplication` | `com.br.itau.login` | Spring Boot entry point |
| `LoginImpl` | `controller` | REST controller implementing `Login` interface |
| `LoginServiceImpl` | `service` | Orchestrates authentication, session creation, JWT issuance |
| `JwtServiceImpl` | `service` | JWT generation and parsing using JJWT |
| `SessionServiceImpl` | `service` | Session CRUD in Redis |
| `LoginRateLimiter` | `service` | Redis-backed distributed rate limiter (Lua script) |
| `ContractServiceImpl` | `service` | Checks if a customer has the service contracted |
| `JwtAuthenticationFilter` | `security` | Validates JWT on every request except `/login` |
| `ContractAuthorizationFilter` | `security` | Enforces `contractService` flag for protected endpoints |
| `LoginRateLimitFilter` | `security` | Applies per-IP rate limiting before authentication |
| `AuthenticationFailureEventListener` | `security` | Logs and handles authentication failure events |
| `UserDetailsServiceImpl` | `security` | Loads `UserDetails` from the H2/PostgreSQL database |

---

## Internal Flow

```
POST /api/v1/login
      │
      ├── LoginRateLimitFilter
      │   └── Redis: INCR rate_limit:login:<ip>
      │       → 429 if count > maxRequests (5)
      │
      ├── Spring Security AuthenticationManager
      │   └── UserDetailsServiceImpl.loadUserByUsername()
      │       → load UserAccount from H2
      │       → verify BCrypt password
      │
      ├── LoginServiceImpl.login()
      │   ├── load UserAccount (role, contractService)
      │   ├── generate sessionId (UUID)
      │   ├── generate symmetricKey (Base64 random)
      │   ├── create SessionDTO { sessionId, username, contractService, key, role }
      │   ├── Redis: SET session:<sessionId> → SessionDTO  (TTL 5 min)
      │   └── JwtServiceImpl.generateToken()
      │       → sign HS256 JWT with { sub: username, sessionId, channel, role }
      │
      └── return AuthResponse { token }


GET /api/v1/me
      │
      ├── JwtAuthenticationFilter
      │   ├── parse JWT from Authorization header
      │   ├── validate signature + expiry
      │   └── set SecurityContext with SessionDTO principal
      │
      └── LoginImpl.me()
          └── return MeResponseDTO { sessionId, username, contractService, role }
```

---

## Security Architecture

### JWT Token

- **Algorithm**: HS256 (HMAC-SHA256) with a 32+ character secret key
- **Expiry**: 5 minutes (`expiration-ms: 300000`)
- **Claims embedded**: `sub` (username/customerId), `sessionId`, `channel`, `role`

### Session Storage (Redis)

Sessions are stored in Redis as serialized `SessionDTO` objects. The `sessionId` embedded in the JWT allows `TransactionService` to call `/me` and receive the full session context without storing credentials.

### Rate Limiting (Redis Lua)

The rate limiter uses a Redis Lua script to atomically INCR a counter and set its TTL on first use. This ensures correctness even when multiple application instances run concurrently (distributed rate limiting without race conditions).

**Fail-open behaviour**: if Redis is unavailable, the rate limiter allows the request through. This is a deliberate choice: availability is prioritised over rate limiting robustness, as a Redis outage should not lock all customers out of login.

---

## Integrations

| Integration | Direction | Protocol | Notes |
|-------------|-----------|----------|-------|
| Redis | Outbound | TCP / Redis protocol | Session storage + rate limiting |
| H2 / PostgreSQL | Outbound | JDBC | User account storage |
| TransactionService | Inbound | HTTP REST | Calls `/me` to validate sessions |

---

## Configuration

Key configuration properties (`application.yaml`):

```yaml
server:
  port: 8081
  servlet:
    context-path: /api/v1

jwt:
  secret: "ChangeThisSecretKeyForProdUseAtLeast32Chars!"
  expiration-ms: 300000  # 5 minutes

rate-limit:
  max-requests: 5
  window-seconds: 60

spring:
  redis:
    host: localhost
    port: 6379
```
