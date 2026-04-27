# =============================================================================
# Itaú Microservices — complete platform Makefile
# =============================================================================

.PHONY: bootstrap up down restart logs health e2e queues clean

# ── One-command bootstrap ─────────────────────────────────────────────────────

## Download the platform (ZIP, no git clone) and start everything — single command
bootstrap:
	@bash setup.sh

# ── Full platform lifecycle ───────────────────────────────────────────────────

## Build images and start the FULL platform (infra + all three services)
up:
	docker compose up -d --build
	@echo ""
	@echo "Platform is starting. Run 'make health' to verify all services."

## Stop all containers
down:
	docker compose down

## Restart everything
restart: down up

## Tail logs from all containers
logs:
	docker compose logs -f

# ── Validation ────────────────────────────────────────────────────────────────

## Check that all containers (infra + services) are healthy
health:
	@echo "==> Redis:"
	@docker exec redis redis-cli ping || echo "Redis not ready"
	@echo "==> MongoDB:"
	@docker exec mongo mongosh --eval "db.adminCommand('ping')" --quiet || echo "MongoDB not ready"
	@echo "==> LocalStack SQS queues:"
	@aws --endpoint-url=http://localhost:4566 sqs list-queues \
		--region us-east-1 \
		--query 'QueueUrls' \
		--output table 2>/dev/null || echo "LocalStack not ready"
	@echo "==> LoginService (8081):"
	@curl -sf http://localhost:8081/actuator/health || echo "LoginService not ready"
	@echo ""
	@echo "==> TransactionService (8080):"
	@curl -sf http://localhost:8080/actuator/health || echo "TransactionService not ready"
	@echo ""
	@echo "==> GameService (8082):"
	@curl -sf http://localhost:8082/actuator/health || echo "GameService not ready"
	@echo ""

## Manually create/recreate the SQS queues (idempotent)
queues:
	@echo "==> Creating SQS DLQ..."
	aws --endpoint-url=http://localhost:4566 sqs create-queue \
		--queue-name transactions-dlq \
		--region us-east-1 2>/dev/null || true
	@echo "==> Creating SQS main queue..."
	aws --endpoint-url=http://localhost:4566 sqs create-queue \
		--queue-name transactions \
		--region us-east-1 2>/dev/null || true
	@echo "==> Done. Queues:"
	aws --endpoint-url=http://localhost:4566 sqs list-queues --region us-east-1

# ── End-to-end test ───────────────────────────────────────────────────────────

## Run the full E2E flow (requires all three services to be running)
## Usage: make e2e  OR  make e2e USERNAME=Thiago PASSWORD=231299
e2e:
	@bash scripts/e2e.sh "$${USERNAME:-customer123}" "$${PASSWORD:-secret}"

# ── Cleanup ───────────────────────────────────────────────────────────────────

## Remove containers and persistent volumes (destructive)
clean:
	docker compose down -v --remove-orphans
