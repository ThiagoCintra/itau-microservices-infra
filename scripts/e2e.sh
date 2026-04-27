#!/usr/bin/env bash
# =============================================================================
# e2e.sh — End-to-end smoke test for the Itaú Microservices platform
#
# Usage:
#   ./scripts/e2e.sh [USERNAME] [PASSWORD]
#
# Defaults:
#   USERNAME = customer123
#   PASSWORD = secret
#
# Prerequisites: curl
# =============================================================================

set -euo pipefail

USERNAME="${1:-customer123}"
PASSWORD="${2:-secret}"

LOGIN_URL="http://localhost:8081/api/v1/login"
ME_URL="http://localhost:8081/api/v1/me"
TRANSACTION_URL="http://localhost:8080/transactions"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}✔ $*${NC}"; }
fail() { echo -e "${RED}✘ $*${NC}"; exit 1; }
info() { echo -e "${YELLOW}➜ $*${NC}"; }

# ── 1. Health checks ─────────────────────────────────────────────────────────
info "Checking service health..."

check_health() {
  local name="$1" url="$2"
  local status
  status=$(curl -sf "${url}" | grep -o '"status":"[^"]*"' | cut -d'"' -f4 || echo "DOWN")
  if [ "${status}" = "UP" ]; then
    pass "${name} is UP"
  else
    fail "${name} health check failed (status=${status}). Is the service running?"
  fi
}

check_health "LoginService"       "http://localhost:8081/actuator/health"
check_health "TransactionService" "http://localhost:8080/actuator/health"
check_health "GameService"        "http://localhost:8082/actuator/health"

# ── 2. Login ─────────────────────────────────────────────────────────────────
info "Logging in as '${USERNAME}'..."

LOGIN_RESPONSE=$(curl -sf -X POST "${LOGIN_URL}" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"${USERNAME}\",\"password\":\"${PASSWORD}\"}")

TOKEN=$(echo "${LOGIN_RESPONSE}" | grep -o '"token":"[^"]*"' | cut -d'"' -f4)

if [ -z "${TOKEN}" ]; then
  fail "Login failed. Response: ${LOGIN_RESPONSE}"
fi

pass "Login successful. JWT obtained."

# ── 3. GET /me ───────────────────────────────────────────────────────────────
info "Validating session via GET /me..."

ME_RESPONSE=$(curl -sf -X GET "${ME_URL}" \
  -H "Authorization: Bearer ${TOKEN}")

pass "/me response: ${ME_RESPONSE}"

# ── 4. POST /transactions ────────────────────────────────────────────────────
IDEMPOTENCY_KEY=$(cat /proc/sys/kernel/random/uuid 2>/dev/null \
  || uuidgen 2>/dev/null \
  || openssl rand -hex 16 2>/dev/null \
  || echo "$(date +%s)-$$")

info "Submitting PIX transaction (idempotency-key=${IDEMPOTENCY_KEY})..."

TX_RESPONSE=$(curl -sf -X POST "${TRANSACTION_URL}" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "X-Idempotency-Key: ${IDEMPOTENCY_KEY}" \
  -d '{"type":"PIX","amount":500.00}')

TX_STATUS=$(echo "${TX_RESPONSE}" | grep -o '"status":"[^"]*"' | cut -d'"' -f4 || echo "")

if [ "${TX_STATUS}" = "ACCEPTED" ]; then
  pass "Transaction accepted: ${TX_RESPONSE}"
else
  fail "Transaction not accepted. Response: ${TX_RESPONSE}"
fi

# ── 5. Wait for GameService to process ───────────────────────────────────────
info "Waiting 5 seconds for GameService to process the SQS event..."
sleep 5

# ── 6. Summary ───────────────────────────────────────────────────────────────
echo ""
pass "═══════════════════════════════════════"
pass " End-to-end flow completed successfully"
pass "═══════════════════════════════════════"
echo ""
echo "  Username    : ${USERNAME}"
echo "  JWT token   : ${TOKEN:0:40}..."
echo "  TX status   : ${TX_STATUS}"
echo "  Idempotency : ${IDEMPOTENCY_KEY}"
echo ""
echo "Inspect GameService results:"
echo "  docker exec -it mongo mongosh --eval 'use game_db; db.game_events.find().pretty()'"
echo "  docker logs game-service"
echo ""
echo "Inspect SQS DLQ for failed messages:"
echo "  aws --endpoint-url=http://localhost:4566 sqs receive-message \\"
echo "    --queue-url http://localhost:4566/000000000000/transactions-dlq"
