# Itaú Microservices — Gamification Platform

This repository is the **infrastructure and documentation hub** for the Itaú Microservices Gamification Platform, a technical challenge demonstrating event-driven microservices architecture on top of a banking transaction system.

---

## Overview

The platform rewards customers for using PIX — Itaú's instant payment product — through a gamification layer that awards points, unlocks levels, and tracks benefit redemptions. The key design principle is that **gamification must never affect the transaction path**: transactions are accepted immediately, and gamification runs asynchronously.

---

## Architecture Summary

```
┌──────────────┐  JWT    ┌──────────────────────┐  SQS Event  ┌──────────────┐
│              │◄───────►│                      │────────────►│              │
│ LoginService │  /me    │  TransactionService  │             │  GameService │
│    :8081     │         │        :8080         │             │    :8082     │
└──────┬───────┘         └──────────────────────┘             └──────┬───────┘
       │                                                              │
   Redis                        LocalStack SQS                  H2 + MongoDB
 (sessions)                  (transactions queue)               (game state)
```

Three independently deployable microservices:

| Service | Responsibility | Stack |
|---------|---------------|-------|
| **LoginService** | Authentication, JWT issuance, session management | Spring Boot · Redis · H2 |
| **TransactionService** | Transaction acceptance, validation, SQS publishing | Spring Boot · AWS SQS · Resilience4j |
| **GameService** | Gamification: missions, levels, benefits (async consumer) | Spring Boot · SQS · MongoDB · H2 |

---

## How to Run

### Prerequisites

- Docker and Docker Compose
- Java 21 (for building from source)
- AWS CLI (for queue inspection; optional)

### Option 1: Central infrastructure docker-compose (recommended)

This repository provides a single `docker-compose.yml` that starts all shared
infrastructure — Redis, MongoDB, and LocalStack (SQS) — with the SQS queues
created automatically on startup.

```bash
# 1. Clone all service repositories
git clone https://github.com/ThiagoCintra/LoginService
git clone https://github.com/ThiagoCintra/TransactionService
git clone https://github.com/ThiagoCintra/GameService

# 2. Start shared infrastructure from THIS repository
cd itau-microservices-infra
make up          # or: docker compose up -d

# 3. Verify infrastructure is healthy
make health

# 4. Copy and adjust environment variables
cp .env.example .env

# 5. Build each service (Java 21+)
cd ../LoginService       && ./mvnw clean package -DskipTests
cd ../TransactionService && ./mvnw clean package -DskipTests
cd ../GameService        && ./mvnw clean package -DskipTests

# 6. Start services (each in its own terminal or as background processes)
cd ../LoginService       && java -jar target/*.jar &
cd ../TransactionService && java -jar target/*.jar &
cd ../GameService        && java -jar target/*.jar &

# 7. Run the end-to-end smoke test
cd ../itau-microservices-infra
make e2e USERNAME=customer123 PASSWORD=secret
```

**Infrastructure components started by `docker compose up -d`:**

| Container  | Port  | Purpose                              |
|------------|-------|--------------------------------------|
| `redis`    | 6379  | Session storage + rate limiting      |
| `mongo`    | 27017 | Game event log (MongoDB `game_db`)   |
| `localstack` | 4566 | AWS SQS emulation (queues auto-created) |

**SQS queues created automatically on LocalStack startup:**

| Queue              | Purpose                                        |
|--------------------|------------------------------------------------|
| `transactions`     | Main event queue (TransactionService → GameService) |
| `transactions-dlq` | Dead Letter Queue (after 5 failed receive attempts) |

### Option 2: Per-service docker-compose files

Each service has its own `docker-compose.yml`. Start the shared infrastructure first, then start each service:

```bash
# 1. Start LoginService (Redis + H2)
cd LoginService
docker-compose up -d

# 2. Start TransactionService (LocalStack SQS + WireMock for LoginService mock)
cd TransactionService
docker-compose up -d

# 3. Start GameService (LocalStack SQS + MongoDB)
cd GameService
docker-compose up -d
```

> The `docker-compose.yml` files in TransactionService and GameService include LocalStack,
> which automatically creates the SQS queues (`transactions` and `transactions-dlq`)
> via an init script on startup.

### Option 3: Run each service locally (development mode)

```bash
# Start Redis
docker run -p 6379:6379 redis:7

# Start LocalStack (SQS)
docker run -p 4566:4566 -e SERVICES=sqs localstack/localstack:3.0

# Start MongoDB
docker run -p 27017:27017 mongo:6.0

# Build and run each service
cd LoginService    && ./mvnw spring-boot:run
cd TransactionService && ./mvnw spring-boot:run
cd GameService     && ./mvnw spring-boot:run
```

---

## Quickstart: Test the Full Flow

### 1. Login and get a JWT token

```bash
curl -X POST http://localhost:8081/api/v1/login \
  -H "Content-Type: application/json" \
  -d '{ "username": "customer123", "password": "secret" }'
```

Response:
```json
{ "token": "eyJhbGciOiJIUzI1NiJ9..." }
```

### 2. Submit a PIX transaction

```bash
TOKEN="eyJhbGciOiJIUzI1NiJ9..."

curl -X POST http://localhost:8080/transactions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Idempotency-Key: $(uuidgen)" \
  -d '{ "type": "PIX", "amount": 500.00 }'
```

Response (202 Accepted):
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

### 3. GameService processes the event asynchronously

Within a few seconds, GameService will:
- Consume the SQS event
- Evaluate mission eligibility (PIX R$ 500 → "PIX Small" → +5 points)
- Update `CustomerProgress` (level and total points)
- Store a `GameEventDocument` in MongoDB

---

## Flow Summary

```
Customer → POST /login → JWT token
Customer → POST /transactions (Bearer JWT) →
  TransactionService validates JWT →
  GET /me from LoginService →
  Publish to SQS →
  202 Accepted to customer
                   ↓ (async)
  GameService consumes from SQS →
  Apply idempotency, monthly reset, missions, level →
  Persist to H2 + MongoDB
```

For the full step-by-step description, see [docs/flow.md](docs/flow.md).

---

## Documentation

Full technical documentation is in the [`/docs`](docs/) folder:

| Document | Link |
|----------|------|
| Business challenge and rules | [docs/challenge.md](docs/challenge.md) |
| Architecture overview | [docs/architecture.md](docs/architecture.md) |
| Technical decisions | [docs/decisions.md](docs/decisions.md) |
| End-to-end flow | [docs/flow.md](docs/flow.md) |
| LoginService | [docs/services/login-service.md](docs/services/login-service.md) |
| TransactionService | [docs/services/transaction-service.md](docs/services/transaction-service.md) |
| GameService | [docs/services/game-service.md](docs/services/game-service.md) |

---

## Repositories

| Repository | Description |
|------------|-------------|
| [ThiagoCintra/LoginService](https://github.com/ThiagoCintra/LoginService) | Authentication service |
| [ThiagoCintra/TransactionService](https://github.com/ThiagoCintra/TransactionService) | Transaction acceptance service |
| [ThiagoCintra/GameService](https://github.com/ThiagoCintra/GameService) | Gamification event consumer |
| [ThiagoCintra/itau-microservices-infra](https://github.com/ThiagoCintra/itau-microservices-infra) | This repository — infrastructure and documentation |