#!/bin/bash
# One-time Google Cloud setup for MarketPulse on Cloud Run
# Run this once from your local machine with gcloud CLI installed and authenticated.
#
# Usage:
#   chmod +x deploy/setup.sh
#   ./deploy/setup.sh YOUR_PROJECT_ID

set -e

PROJECT_ID="${1:-my-marketpulse-project}"
REGION="asia-south1"          # Mumbai — lowest latency for India
SERVICE="marketpulse"
IMAGE="gcr.io/$PROJECT_ID/$SERVICE"

echo "==> Setting project: $PROJECT_ID"
gcloud config set project "$PROJECT_ID"

echo "==> Enabling APIs"
gcloud services enable \
  run.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  containerregistry.googleapis.com

echo "==> Creating Firestore database (native mode)"
gcloud firestore databases create --location=asia-south1 2>/dev/null || echo "  (already exists)"

echo "==> Storing Kite secrets in Secret Manager"
echo "Enter your Kite API Key:"
read -s KITE_API_KEY
echo "$KITE_API_KEY" | gcloud secrets create KITE_API_KEY --data-file=- 2>/dev/null || \
  echo "$KITE_API_KEY" | gcloud secrets versions add KITE_API_KEY --data-file=-

echo "Enter your Kite API Secret:"
read -s KITE_API_SECRET
echo "$KITE_API_SECRET" | gcloud secrets create KITE_API_SECRET --data-file=- 2>/dev/null || \
  echo "$KITE_API_SECRET" | gcloud secrets versions add KITE_API_SECRET --data-file=-

echo "==> Building and pushing Docker image"
gcloud builds submit --tag "$IMAGE" .

echo "==> Deploying to Cloud Run"
gcloud run deploy "$SERVICE" \
  --image "$IMAGE" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --memory 1Gi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 3 \
  --timeout 300 \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT_ID" \
  --set-secrets "KITE_API_KEY=KITE_API_KEY:latest,KITE_API_SECRET=KITE_API_SECRET:latest"

echo ""
echo "==> Done! Your app URL:"
gcloud run services describe "$SERVICE" --region "$REGION" --format="value(status.url)"
