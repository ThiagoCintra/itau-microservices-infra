#!/usr/bin/env python3
"""
LoginService — authentication, JWT issuance and session management.

Endpoints:
  POST /api/v1/login   — validate credentials, issue JWT, store session in Redis
  GET  /api/v1/me      — return active session from Redis (or reconstructed from JWT)
  GET  /actuator/health — liveness probe
"""

import datetime
import json
import logging
import os
import uuid

import jwt
import redis
from flask import Flask, jsonify, request

# ── Configuration ─────────────────────────────────────────────────────────────
JWT_SECRET         = os.environ.get("JWT_SECRET",          "ChangeThisSecretKeyForProdUseAtLeast32Chars!")
JWT_EXPIRY_SECONDS = int(os.environ.get("JWT_EXPIRY_SECONDS", "300"))
REDIS_HOST         = os.environ.get("REDIS_HOST",          "localhost")
REDIS_PORT         = int(os.environ.get("REDIS_PORT",       "6379"))
SERVER_PORT        = int(os.environ.get("SERVER_PORT",      "8081"))

# ── User store (BCrypt not required for mock) ─────────────────────────────────
# Supports both the default e2e credentials and the credentials from the
# problem statement (Thiago / 231299).
USERS = {
    "customer123": {"password": "secret",  "role": "CUSTOMER", "contractService": True},
    "Thiago":      {"password": "231299",  "role": "CUSTOMER", "contractService": True},
    "admin":       {"password": "admin",   "role": "ADMIN",    "contractService": True},
}

# ── App setup ─────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [login-service] %(levelname)s %(message)s")
app = Flask(__name__)


def _redis():
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True, socket_timeout=2)


def _decode_bearer(auth_header: str) -> dict:
    """Validate Authorization header and return decoded JWT payload."""
    if not auth_header.startswith("Bearer "):
        raise ValueError("Missing Bearer token")
    token = auth_header[7:]
    return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/actuator/health", methods=["GET"])
def health():
    return jsonify({"status": "UP"})


@app.route("/api/v1/login", methods=["POST"])
def login():
    data     = request.get_json(force=True, silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")

    user = USERS.get(username)
    if not user or user["password"] != password:
        app.logger.warning("Failed login attempt for user '%s'", username)
        return jsonify({"error": "Invalid credentials"}), 401

    session_id = str(uuid.uuid4())
    session = {
        "sessionId":       session_id,
        "username":        username,
        "contractService": user["contractService"],
        "role":            user["role"],
    }

    # Persist session in Redis with 5-minute TTL (fail-open if Redis is down)
    try:
        _redis().setex(f"session:{session_id}", JWT_EXPIRY_SECONDS, json.dumps(session))
    except Exception as exc:
        app.logger.warning("Redis unavailable, proceeding without session storage: %s", exc)

    now = datetime.datetime.utcnow()
    payload = {
        "sub":       username,
        "sessionId": session_id,
        "channel":   "MOBILE",
        "role":      user["role"],
        "iat":       now,
        "exp":       now + datetime.timedelta(seconds=JWT_EXPIRY_SECONDS),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")

    app.logger.info("Login OK — user='%s' sessionId='%s'", username, session_id)
    return jsonify({"token": token})


@app.route("/api/v1/me", methods=["GET"])
def me():
    auth = request.headers.get("Authorization", "")
    try:
        payload = _decode_bearer(auth)
    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Token expired"}), 401
    except Exception:
        return jsonify({"error": "Invalid token"}), 401

    session_id = payload.get("sessionId")
    username   = payload.get("sub")

    # Try Redis first
    try:
        raw = _redis().get(f"session:{session_id}")
        if raw:
            return jsonify(json.loads(raw))
    except Exception as exc:
        app.logger.warning("Redis unavailable, reconstructing session from JWT: %s", exc)

    # Fall back: reconstruct from JWT claims (covers Redis-unavailable and just-restarted scenarios)
    user = USERS.get(username, {})
    return jsonify({
        "sessionId":       session_id,
        "username":        username,
        "contractService": user.get("contractService", True),
        "role":            payload.get("role", "CUSTOMER"),
    })


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=SERVER_PORT, threaded=True)
