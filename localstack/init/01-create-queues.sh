#!/usr/bin/env bash
# LocalStack init script — runs automatically when LocalStack is ready.
# Creates the SQS queues required by TransactionService and GameService.

set -euo pipefail

REGION="us-east-1"
ENDPOINT="http://localhost:4566"

echo "==> Creating SQS queues..."

# Dead Letter Queue must be created first so we can reference its ARN
awslocal sqs create-queue \
  --queue-name transactions-dlq \
  --region "${REGION}"

DLQ_ARN=$(awslocal sqs get-queue-attributes \
  --queue-url "${ENDPOINT}/000000000000/transactions-dlq" \
  --attribute-names QueueArn \
  --region "${REGION}" \
  --query 'Attributes.QueueArn' \
  --output text)

echo "==> DLQ ARN: ${DLQ_ARN}"

# Main queue — wired to the DLQ after 5 failed receive attempts
REDRIVE_POLICY=$(printf '{"deadLetterTargetArn":"%s","maxReceiveCount":"5"}' "${DLQ_ARN}")

awslocal sqs create-queue \
  --queue-name transactions \
  --region "${REGION}" \
  --attributes "RedrivePolicy=${REDRIVE_POLICY}"

echo "==> SQS queues ready:"
awslocal sqs list-queues --region "${REGION}"
