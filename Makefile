# =============================================================================
# Itaú Microservices — central infrastructure Makefile
# =============================================================================

.PHONY: up down restart logs health e2e queues clean

# ── Infrastructure lifecycle ──────────────────────────────────────────────────

## Start all shared infrastructure containers (Redis, MongoDB, LocalStack)
up:
	docker compose up -d
	@echo ""
	@echo "Infrastructure is starting. Run 'make health' to verify."

## Stop all infrastructure containers
down:
	docker compose down

## Restart all infrastructure containers
restart: down up

## Tail logs from all infrastructure containers
logs:
	docker compose logs -f

# ── Validation ────────────────────────────────────────────────────────────────

## Check that all infrastructure containers are healthy
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
	@bash scripts/e2e.sh ${USERNAME} ${PASSWORD}

# ── Cleanup ───────────────────────────────────────────────────────────────────

## Remove containers and persistent volumes (destructive)
clean:
	docker compose down -v --remove-orphans
