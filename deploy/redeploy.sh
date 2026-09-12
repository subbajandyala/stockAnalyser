#!/bin/bash
# Rebuild and redeploy after code changes.
# Usage: ./deploy/redeploy.sh YOUR_PROJECT_ID

set -e

PROJECT_ID="${1:-my-marketpulse-project}"
REGION="asia-south1"
SERVICE="marketpulse"
IMAGE="gcr.io/$PROJECT_ID/$SERVICE"

gcloud config set project "$PROJECT_ID"
gcloud builds submit --tag "$IMAGE" .
gcloud run deploy "$SERVICE" \
  --image "$IMAGE" \
  --region "$REGION" \
  --platform managed

echo "Redeployed. URL:"
gcloud run services describe "$SERVICE" --region "$REGION" --format="value(status.url)"
