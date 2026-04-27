#!/usr/bin/env bash
# =============================================================================
# setup.sh — One-command bootstrap for the Itaú Microservices platform
#
# Usage (from a fresh environment — no git clone required):
#
#   curl -fsSL \
#     https://raw.githubusercontent.com/ThiagoCintra/itau-microservices-infra/main/setup.sh \
#     | bash
#
# Or, if you already have the repository:
#
#   bash setup.sh
#
# What this script does:
#   1. Verifies prerequisites (Docker + Docker Compose)
#   2. Downloads the itau-microservices-infra repo as a ZIP via curl
#      (respects the requirement: no git clone)
#   3. Starts the full platform with:  docker compose up -d --build
#   4. Waits for all six containers to report healthy
#   5. Prints a summary with service URLs and quick-start curl examples
#
# Optional environment variables:
#   SKIP_DOWNLOAD=1   — skip ZIP download (use when already inside the repo)
#   SKIP_E2E=1        — skip the end-to-end smoke test at the end
#   USERNAME          — e2e test username  (default: customer123)
#   PASSWORD          — e2e test password  (default: secret)
# =============================================================================

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${CYAN}[setup]${NC} $*"; }
success() { echo -e "${GREEN}✔${NC} $*"; }
warn()    { echo -e "${YELLOW}⚠${NC}  $*"; }
error()   { echo -e "${RED}✘ $*${NC}"; exit 1; }

# ── Config ────────────────────────────────────────────────────────────────────
REPO_OWNER="ThiagoCintra"
REPO_NAME="itau-microservices-infra"
BRANCH="main"
ZIP_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/refs/heads/${BRANCH}.zip"
WORK_DIR="${REPO_NAME}"

HEALTH_TIMEOUT=180   # seconds to wait for all services to become healthy
HEALTH_INTERVAL=5    # seconds between health poll cycles

# ── 1. Prerequisite checks ────────────────────────────────────────────────────
info "Checking prerequisites..."

if ! command -v docker &>/dev/null; then
  error "Docker is not installed. Install it from https://docs.docker.com/get-docker/ and re-run."
fi

if docker compose version &>/dev/null 2>&1; then
  COMPOSE_CMD="docker compose"
elif command -v docker-compose &>/dev/null; then
  COMPOSE_CMD="docker-compose"
else
  error "Docker Compose (v2 plugin or standalone docker-compose) is not installed."
fi

success "Docker $(docker --version | awk '{print $3}' | tr -d ,) and Compose are available"

# ── 2. Download repo as ZIP (unless we are already inside it) ─────────────────
if [[ "${SKIP_DOWNLOAD:-0}" == "1" ]] || [[ -f "docker-compose.yml" && -d "services" ]]; then
  info "Already inside the repository — skipping ZIP download."
  WORK_DIR="."
else
  info "Downloading ${REPO_OWNER}/${REPO_NAME} as ZIP (no git clone)..."

  if [[ -d "${WORK_DIR}" ]]; then
    warn "Directory '${WORK_DIR}' already exists — removing stale copy..."
    rm -rf "${WORK_DIR}"
  fi

  TMP_ZIP=$(mktemp /tmp/itau-infra-XXXXXX.zip)
  # Download: curl -L follows redirects (GitHub ZIP redirect chain)
  curl -fsSL "${ZIP_URL}" -o "${TMP_ZIP}" \
    || error "Failed to download ${ZIP_URL}. Check connectivity and try again."

  info "Extracting ZIP..."
  # GitHub ZIPs contain a single top-level directory: <repo>-<branch>/
  EXTRACT_DIR=$(mktemp -d /tmp/itau-infra-extract-XXXXXX)
  unzip -q "${TMP_ZIP}" -d "${EXTRACT_DIR}"
  rm -f "${TMP_ZIP}"

  # Move extracted contents to WORK_DIR
  INNER_DIR=$(find "${EXTRACT_DIR}" -maxdepth 1 -mindepth 1 -type d | head -1)
  mv "${INNER_DIR}" "${WORK_DIR}"
  rm -rf "${EXTRACT_DIR}"

  success "Repository extracted to ./${WORK_DIR}"
  cd "${WORK_DIR}"
fi

# ── 3. Start the platform ─────────────────────────────────────────────────────
info "Starting the full platform (infra + all three services)..."
info "Command: ${COMPOSE_CMD} up -d --build"
echo ""

${COMPOSE_CMD} up -d --build

echo ""
success "All containers started. Waiting for health checks..."

# ── 4. Wait for all services to be healthy ────────────────────────────────────

SERVICES=(
  "login-service|http://localhost:8081/actuator/health"
  "transaction-service|http://localhost:8080/actuator/health"
  "game-service|http://localhost:8082/actuator/health"
)

INFRA=(
  "redis|redis-cli ping|PONG"
  "mongo|mongosh --eval db.adminCommand\\(\\''ping\\'\\) --quiet|ok"
)

_service_healthy() {
  local url="$1"
  local status
  status=$(curl -sf --max-time 3 "${url}" 2>/dev/null \
    | grep -o '"status":"[^"]*"' \
    | cut -d'"' -f4 || echo "")
  [[ "${status}" == "UP" ]]
}

elapsed=0
all_healthy=false

while [[ ${elapsed} -lt ${HEALTH_TIMEOUT} ]]; do
  all_healthy=true
  for entry in "${SERVICES[@]}"; do
    name="${entry%%|*}"
    url="${entry##*|}"
    if ! _service_healthy "${url}"; then
      all_healthy=false
      break
    fi
  done

  if ${all_healthy}; then
    break
  fi

  sleep ${HEALTH_INTERVAL}
  elapsed=$(( elapsed + HEALTH_INTERVAL ))
  info "Waiting for services... (${elapsed}s / ${HEALTH_TIMEOUT}s)"
done

echo ""
if ! ${all_healthy}; then
  warn "Some services did not become healthy within ${HEALTH_TIMEOUT}s."
  warn "Check logs with:  ${COMPOSE_CMD} logs -f"
  warn "Or run:           make health"
else
  success "All services are healthy!"
fi

# ── 5. Summary ────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}═══════════════════════════════════════════════════════${NC}"
echo -e "${BOLD} Itaú Microservices Platform is ready!${NC}"
echo -e "${BOLD}═══════════════════════════════════════════════════════${NC}"
echo ""
echo "  Service              URL"
echo "  ─────────────────    ──────────────────────────────────────────"
echo "  LoginService         http://localhost:8081"
echo "  TransactionService   http://localhost:8080"
echo "  GameService          http://localhost:8082"
echo "  LocalStack (SQS)     http://localhost:4566"
echo "  Redis                localhost:6379"
echo "  MongoDB              localhost:27017"
echo ""
echo -e "${BOLD}Quick-start:${NC}"
echo ""
echo "  # 1. Login"
echo "  curl -s -X POST http://localhost:8081/api/v1/login \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"username\":\"customer123\",\"password\":\"secret\"}'"
echo ""
echo "  # 2. Submit a PIX transaction (replace <TOKEN>)"
echo "  curl -s -X POST http://localhost:8080/transactions \\"
echo "    -H 'Authorization: Bearer <TOKEN>' \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -H 'X-Idempotency-Key: \$(uuidgen)' \\"
echo "    -d '{\"type\":\"PIX\",\"amount\":500.00}'"
echo ""
echo -e "${BOLD}Makefile targets:${NC}  make health | make e2e | make logs | make down"
echo ""

# ── 6. Optional E2E smoke test ────────────────────────────────────────────────
if [[ "${SKIP_E2E:-0}" != "1" ]] && ${all_healthy}; then
  info "Running end-to-end smoke test..."
  echo ""
  bash scripts/e2e.sh "${USERNAME:-customer123}" "${PASSWORD:-secret}" || true
fi
