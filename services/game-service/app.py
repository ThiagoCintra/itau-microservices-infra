#!/usr/bin/env python3
"""
GameService — asynchronous SQS consumer and gamification engine.

Background thread polls the SQS `transactions` queue. For each PIX event it:
  - Checks idempotency (MongoDB game_events, then in-memory processed-events set)
  - Evaluates mission eligibility (amount ranges → point awards)
  - Recalculates customer level
  - Persists customer_progress and game_event documents to MongoDB

HTTP endpoints (actuator only — no transaction ingestion by design):
  GET /actuator/health   — liveness probe
  GET /actuator/metrics  — basic stats
"""

import datetime
import json
import logging
import os
import threading
import time

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError
from flask import Flask, jsonify
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

# ── Configuration ─────────────────────────────────────────────────────────────
SQS_QUEUE_URL   = os.environ.get("SQS_QUEUE_URL",  "http://localhost:4566/000000000000/transactions")
SQS_DLQ_URL     = os.environ.get("SQS_DLQ_URL",    "http://localhost:4566/000000000000/transactions-dlq")
AWS_REGION      = os.environ.get("AWS_REGION",      "us-east-1")
AWS_ENDPOINT    = os.environ.get("AWS_ENDPOINT",    "http://localhost:4566")
AWS_ACCESS_KEY  = os.environ.get("AWS_ACCESS_KEY_ID",    "test")
AWS_SECRET_KEY  = os.environ.get("AWS_SECRET_ACCESS_KEY", "test")

MONGO_HOST      = os.environ.get("SPRING_DATA_MONGODB_HOST",     "localhost")
MONGO_PORT      = int(os.environ.get("SPRING_DATA_MONGODB_PORT", "27017"))
MONGO_DB        = os.environ.get("SPRING_DATA_MONGODB_DATABASE", "game_db")

SERVER_PORT     = int(os.environ.get("SERVER_PORT", "8082"))
MAX_RECEIVE     = int(os.environ.get("SQS_MAX_RECEIVE_COUNT", "5"))
# Maximum SQS visibility timeout: 12 hours (AWS hard limit is 43 200 seconds)
_MAX_VISIBILITY_S = 43_200

# ── Seeded game data (mirrors BootstrapDataInitializer in the real service) ───
MISSIONS = [
    {"id": 1, "name": "PIX Small",  "minValue":     0.01, "maxValue":  1_000.00, "points":  5},
    {"id": 2, "name": "PIX Medium", "minValue": 1_000.00, "maxValue":  9_999.00, "points": 10},
    {"id": 3, "name": "PIX Large",  "minValue": 10_000.00,"maxValue": float("inf"), "points": 20},
]
LEVEL_RULES = [
    {"level": 1, "minPoints":     0},
    {"level": 2, "minPoints":   100},
    {"level": 3, "minPoints":   500},
    {"level": 4, "minPoints": 1_000},
    {"level": 5, "minPoints": 2_000},
]

# ── App setup ─────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [game-service] %(levelname)s %(message)s")
app = Flask(__name__)

# In-memory state (equivalent to H2 tables in the real service)
_customer_progress: dict   = {}   # customerId → progress dict
_mission_completions: set  = set() # (customerId, missionId) → seen
_processed_events: set     = set() # eventId → bool (second idempotency layer)
_stats = {"processed": 0, "skipped_duplicate": 0, "dlq": 0}


# ── Infrastructure helpers ─────────────────────────────────────────────────────

def _sqs():
    return boto3.client(
        "sqs",
        region_name=AWS_REGION,
        endpoint_url=AWS_ENDPOINT,
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY,
    )


def _mongo_db():
    client = MongoClient(
        host=MONGO_HOST, port=MONGO_PORT,
        serverSelectionTimeoutMS=5_000,
    )
    return client[MONGO_DB]


# ── Gamification helpers ──────────────────────────────────────────────────────

def _calculate_level(total_points: int) -> int:
    level = 1
    for rule in LEVEL_RULES:
        if total_points >= rule["minPoints"]:
            level = rule["level"]
    return level


def _current_month() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m")


# ── Core event processing ─────────────────────────────────────────────────────

def process_event(event: dict) -> None:
    """Process a single TransactionEvent and apply gamification rules."""
    event_id    = event.get("eventId")
    customer_id = event.get("customerId")
    tx_type     = event.get("type")
    amount      = float(event.get("amount", 0))
    timestamp   = event.get("timestamp", datetime.datetime.utcnow().isoformat() + "Z")

    # ── Idempotency layer 1 — MongoDB ─────────────────────────────────────────
    try:
        db = _mongo_db()
        if db.game_events.find_one({"_id": event_id}):
            app.logger.info("Duplicate (MongoDB) — skipping event %s", event_id)
            _stats["skipped_duplicate"] += 1
            return
        db.game_events.insert_one({
            "_id":         event_id,
            "eventId":     event_id,
            "customerId":  customer_id,
            "type":        tx_type,
            "amount":      amount,
            "timestamp":   timestamp,
            "processedAt": datetime.datetime.utcnow().isoformat() + "Z",
        })
    except Exception as exc:
        app.logger.error("MongoDB error during idempotency check: %s", exc)

    # ── Idempotency layer 2 — in-memory (H2 equivalent) ──────────────────────
    if event_id in _processed_events:
        app.logger.info("Duplicate (in-memory) — skipping event %s", event_id)
        _stats["skipped_duplicate"] += 1
        return

    # ── Only PIX transactions trigger gamification ────────────────────────────
    if tx_type != "PIX":
        app.logger.info("Non-PIX event %s (type=%s) — skipping gamification", event_id, tx_type)
        _processed_events.add(event_id)
        return

    # ── Get or create customer progress ──────────────────────────────────────
    progress = _customer_progress.get(customer_id, {
        "customerId":  customer_id,
        "totalPoints": 0,
        "level":       1,
        "lastReset":   _current_month(),
    })

    # ── Monthly reset ─────────────────────────────────────────────────────────
    if progress["lastReset"] != _current_month():
        app.logger.info("Monthly reset for customer %s", customer_id)
        progress.update({"totalPoints": 0, "level": 1, "lastReset": _current_month()})
        # Clear this customer's mission completions
        for key in list(_mission_completions):
            if key[0] == customer_id:
                _mission_completions.discard(key)

    # ── Evaluate missions ─────────────────────────────────────────────────────
    awarded_missions = []
    for mission in MISSIONS:
        mk = (customer_id, mission["id"])
        if mk in _mission_completions:
            continue
        if mission["minValue"] <= amount <= mission["maxValue"]:
            progress["totalPoints"] += mission["points"]
            _mission_completions.add(mk)
            awarded_missions.append(mission["name"])
            app.logger.info(
                "Customer %s completed mission '%s' (+%d pts) — total=%d",
                customer_id, mission["name"], mission["points"], progress["totalPoints"],
            )

    # ── Recalculate level ─────────────────────────────────────────────────────
    progress["level"] = _calculate_level(progress["totalPoints"])
    _customer_progress[customer_id] = progress

    # ── Persist to MongoDB ────────────────────────────────────────────────────
    try:
        db = _mongo_db()
        db.customer_progress.replace_one(
            {"customerId": customer_id},
            {
                "customerId":      customer_id,
                "totalPoints":     progress["totalPoints"],
                "level":           progress["level"],
                "lastReset":       progress["lastReset"],
                "awardedMissions": awarded_missions,
                "updatedAt":       datetime.datetime.utcnow().isoformat() + "Z",
            },
            upsert=True,
        )
        app.logger.info(
            "Saved progress for customer %s — totalPoints=%d level=%d",
            customer_id, progress["totalPoints"], progress["level"],
        )
    except Exception as exc:
        app.logger.error("MongoDB save failed: %s", exc)

    _processed_events.add(event_id)
    _stats["processed"] += 1


# ── SQS polling loop (runs in a daemon thread) ────────────────────────────────

def _wait_for_sqs() -> object:
    """Block until the SQS queue is reachable and the queue exists."""
    while True:
        try:
            client = _sqs()
            client.get_queue_url(QueueName="transactions")
            app.logger.info("SQS queue 'transactions' is ready")
            return client
        except Exception as exc:
            app.logger.warning("Waiting for SQS: %s — retrying in 5s", exc)
            time.sleep(5)


def sqs_polling_loop() -> None:
    sqs = _wait_for_sqs()
    app.logger.info("SQS polling started — queue=%s", SQS_QUEUE_URL)

    while True:
        try:
            resp = sqs.receive_message(
                QueueUrl=SQS_QUEUE_URL,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=5,
                AttributeNames=["ApproximateReceiveCount"],
            )
        except Exception as exc:
            app.logger.error("SQS receive error: %s — retrying in 5s", exc)
            time.sleep(5)
            sqs = _sqs()
            continue

        for msg in resp.get("Messages", []):
            receipt = msg["ReceiptHandle"]
            body    = msg.get("Body", "{}")
            receive_count = int(msg.get("Attributes", {}).get("ApproximateReceiveCount", "1"))

            try:
                event = json.loads(body)
                process_event(event)
                sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=receipt)
            except Exception as exc:
                app.logger.error("Failed to process message: %s", exc)
                if receive_count >= MAX_RECEIVE:
                    app.logger.warning("Message exceeded max retries (%d), routing to DLQ", MAX_RECEIVE)
                    try:
                        sqs.send_message(QueueUrl=SQS_DLQ_URL, MessageBody=body)
                        sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=receipt)
                        _stats["dlq"] += 1
                    except Exception as dlq_exc:
                        app.logger.error("DLQ routing failed: %s", dlq_exc)
                else:
                    backoff = min(60 * receive_count, _MAX_VISIBILITY_S)
                    app.logger.info("Extending visibility by %ds (attempt %d)", backoff, receive_count)
                    try:
                        sqs.change_message_visibility(
                            QueueUrl=SQS_QUEUE_URL,
                            ReceiptHandle=receipt,
                            VisibilityTimeout=backoff,
                        )
                    except Exception:
                        pass


# ── HTTP endpoints ────────────────────────────────────────────────────────────

@app.route("/actuator/health", methods=["GET"])
def health():
    return jsonify({"status": "UP"})


@app.route("/actuator/metrics", methods=["GET"])
def metrics():
    return jsonify({
        "status":             "UP",
        "eventsProcessed":    _stats["processed"],
        "duplicatesSkipped":  _stats["skipped_duplicate"],
        "sentToDlq":          _stats["dlq"],
        "customersTracked":   len(_customer_progress),
    })


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=sqs_polling_loop, daemon=True, name="sqs-poller").start()
    app.run(host="0.0.0.0", port=SERVER_PORT, threaded=True)
