# Itaú Microservices — Gamification Platform

This repository is the **infrastructure and documentation hub** for the Itaú Microservices Gamification Platform, a technical challenge demonstrating event-driven microservices architecture on top of a banking transaction system.

---

## ⚡ One-Command Bootstrap (no `git clone` required)

Run the entire platform — infrastructure and all three services — with a single command:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/ThiagoCintra/itau-microservices-infra/main/setup.sh \
  | bash
```

`setup.sh` will:
1. Download this repository as a ZIP via `curl` (no `git clone`)
2. Build and start all containers with `docker compose up -d --build`
3. Wait until every service is healthy
4. Print service URLs and quick-start curl examples
5. Run the end-to-end smoke test automatically

**Prerequisites:** Docker (with Compose v2 plugin or standalone `docker-compose`), `curl`, and `unzip` (standard on macOS/Linux; pre-installed in GitHub Codespaces).

> If you already have the repository on disk, run `make bootstrap` (or `bash setup.sh`) from the repo root instead.

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

- Docker (with Compose v2 plugin or standalone `docker-compose`)
- `curl` and `unzip` (standard on macOS/Linux; available in GitHub Codespaces)
- AWS CLI (for queue inspection; optional)

### Option 1: Single-command bootstrap — recommended ✅

```bash
# Download and run the full platform with one command (no git clone)
curl -fsSL \
  https://raw.githubusercontent.com/ThiagoCintra/itau-microservices-infra/main/setup.sh \
  | bash
```

The script downloads this repository as a ZIP, builds all Docker images, starts
every container, waits for health checks, and runs the e2e smoke test.

### Option 2: Manual setup from inside the repository

If you have already downloaded or extracted the repository:

```bash
# Build images and start the full platform
make bootstrap      # equivalent to: bash setup.sh (auto-skips ZIP download)

# — or, if you just want to start containers —
make up             # docker compose up -d --build

# Verify all services are healthy
make health

# Run the end-to-end smoke test
make e2e USERNAME=customer123 PASSWORD=secret
```

### Option 3: Download repo ZIP manually, then compose up

```bash
# 1. Download the repository as a ZIP (no git clone)
curl -L \
  https://github.com/ThiagoCintra/itau-microservices-infra/archive/refs/heads/main.zip \
  -o itau-microservices-infra.zip

# 2. Extract
unzip itau-microservices-infra.zip
cd itau-microservices-infra-main

# 3. Start the platform
docker compose up -d --build

# 4. Wait and verify
make health
```

**Infrastructure components started by `docker compose up -d`:**

| Container  | Port  | Purpose                              |
|------------|-------|--------------------------------------|
| `redis`    | 6379  | Session storage + rate limiting      |
| `mongo`    | 27017 | Game event log (MongoDB `game_db`)   |
| `localstack` | 4566 | AWS SQS emulation (queues auto-created) |
| `login-service` | 8081 | Authentication + JWT (Python/Flask) |
| `transaction-service` | 8080 | Transaction gateway (Python/Flask) |
| `game-service` | 8082 | Gamification consumer (Python/Flask) |

**SQS queues created automatically on LocalStack startup:**

| Queue              | Purpose                                        |
|--------------------|------------------------------------------------|
| `transactions`     | Main event queue (TransactionService → GameService) |
| `transactions-dlq` | Dead Letter Queue (after 5 failed receive attempts) |

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