#!/usr/bin/env python3
"""
TransactionService — transaction acceptance gateway.

Endpoints:
  POST /transactions    — validate JWT, call LoginService /me, publish event to SQS, return 202
  GET  /actuator/health — liveness probe
"""

import datetime
import json
import logging
import os
import uuid

import boto3
import jwt
import requests
from botocore.exceptions import ClientError, EndpointConnectionError
from flask import Flask, jsonify, request

# ── Configuration ─────────────────────────────────────────────────────────────
JWT_SECRET          = os.environ.get("JWT_SECRET",          "ChangeThisSecretKeyForProdUseAtLeast32Chars!")
LOGIN_SERVICE_URL   = os.environ.get("LOGIN_SERVICE_URL",   "http://localhost:8081")
SQS_QUEUE_URL       = os.environ.get("SQS_QUEUE_URL",       "http://localhost:4566/000000000000/transactions")
AWS_REGION          = os.environ.get("AWS_REGION",          "us-east-1")
AWS_ENDPOINT        = os.environ.get("AWS_ENDPOINT",        "http://localhost:4566")
AWS_ACCESS_KEY_ID   = os.environ.get("AWS_ACCESS_KEY_ID",   "test")
AWS_SECRET_KEY      = os.environ.get("AWS_SECRET_ACCESS_KEY","test")
SERVER_PORT         = int(os.environ.get("SERVER_PORT",      "8080"))
LOGIN_TIMEOUT_S     = float(os.environ.get("LOGIN_SERVICE_TIMEOUT_MILLIS", "2000")) / 1000

# ── App setup ─────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [transaction-service] %(levelname)s %(message)s")
app = Flask(__name__)


def _sqs():
    return boto3.client(
        "sqs",
        region_name=AWS_REGION,
        endpoint_url=AWS_ENDPOINT,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_KEY,
    )


def _decode_bearer(auth_header: str) -> dict:
    if not auth_header.startswith("Bearer "):
        raise ValueError("Missing Bearer token")
    return jwt.decode(auth_header[7:], JWT_SECRET, algorithms=["HS256"])


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/actuator/health", methods=["GET"])
def health():
    return jsonify({"status": "UP"})


@app.route("/transactions", methods=["POST"])
def create_transaction():
    # 1. Validate JWT locally
    auth = request.headers.get("Authorization", "")
    try:
        payload = _decode_bearer(auth)
    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Token expired"}), 401
    except Exception:
        return jsonify({"error": "Unauthorized"}), 401

    # 2. Channel must be MOBILE
    channel = payload.get("channel", "")
    if channel != "MOBILE":
        return jsonify({"error": "Forbidden: channel must be MOBILE"}), 403

    # 3. Call LoginService /me to validate active session
    try:
        me_resp = requests.get(
            f"{LOGIN_SERVICE_URL}/api/v1/me",
            headers={"Authorization": auth},
            timeout=LOGIN_TIMEOUT_S,
        )
        if me_resp.status_code != 200:
            app.logger.warning("LoginService /me returned %s", me_resp.status_code)
            return jsonify({"error": "Unauthorized"}), 401
        session = me_resp.json()
    except requests.RequestException as exc:
        app.logger.error("LoginService unreachable: %s", exc)
        return jsonify({"error": "Service unavailable — LoginService"}), 503

    # 4. contractService must be true
    if not session.get("contractService"):
        return jsonify({"error": "Forbidden: contractService is false"}), 403

    # 5. Parse request body
    data   = request.get_json(force=True, silent=True) or {}
    tx_type = data.get("type")
    amount  = data.get("amount")
    if not tx_type or amount is None:
        return jsonify({"error": "Bad request: 'type' and 'amount' are required"}), 400

    idempotency_key = request.headers.get("X-Idempotency-Key") or str(uuid.uuid4())
    customer_id     = payload["sub"]
    now_iso         = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    event = {
        "eventId":    idempotency_key,
        "customerId": customer_id,
        "type":       tx_type,
        "amount":     float(amount),
        "channel":    channel,
        "timestamp":  now_iso,
    }

    # 6. Publish to SQS (fire-and-forget — return 202 even if SQS is momentarily slow)
    try:
        _sqs().send_message(QueueUrl=SQS_QUEUE_URL, MessageBody=json.dumps(event))
        app.logger.info("Published event %s to SQS — customer=%s type=%s amount=%s",
                        idempotency_key, customer_id, tx_type, amount)
    except (ClientError, EndpointConnectionError) as exc:
        app.logger.error("SQS publish failed: %s", exc)
    except Exception as exc:
        app.logger.error("Unexpected SQS error: %s", exc)

    return jsonify({
        "transactionId": idempotency_key,
        "customerId":    customer_id,
        "type":          tx_type,
        "amount":        float(amount),
        "status":        "ACCEPTED",
        "timestamp":     now_iso,
    }), 202


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=SERVER_PORT, threaded=True)
