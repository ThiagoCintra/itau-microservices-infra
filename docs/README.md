# Documentation Index

Welcome to the technical documentation for the **Itaú Microservices Gamification Platform**.

---

## Contents

| Document | Description |
|----------|-------------|
| [challenge.md](challenge.md) | Business problem, gamification scenario, and rules |
| [architecture.md](architecture.md) | System architecture, patterns, and component overview |
| [decisions.md](decisions.md) | Technical decisions and trade-off analysis |
| [flow.md](flow.md) | End-to-end request flow with step-by-step description |
| [services/login-service.md](services/login-service.md) | LoginService deep-dive |
| [services/transaction-service.md](services/transaction-service.md) | TransactionService deep-dive |
| [services/game-service.md](services/game-service.md) | GameService deep-dive |

---

## Quick Reference

### Service Ports

| Service              | Port |
|----------------------|------|
| LoginService         | 8081 |
| TransactionService   | 8080 |
| GameService          | 8082 |

### Key Endpoints

| Endpoint | Service | Description |
|----------|---------|-------------|
| `POST /api/v1/login` | LoginService | Authenticate and get JWT |
| `GET /api/v1/me` | LoginService | Validate active session |
| `POST /transactions` | TransactionService | Submit a PIX transaction |

### Infrastructure (Local)

| Component | Port |
|-----------|------|
| Redis | 6379 |
| MongoDB | 27017 |
| LocalStack (SQS) | 4566 |

---

## How to Read This Documentation

1. **Start with [challenge.md](challenge.md)** to understand the business problem and rules.
2. **Read [architecture.md](architecture.md)** for the high-level design and the rationale for event-driven architecture.
3. **Review [decisions.md](decisions.md)** to understand each technology choice and its trade-offs.
4. **Follow [flow.md](flow.md)** to trace a transaction from login to gamification update.
5. **Explore the service docs** for implementation-level details on each microservice.
