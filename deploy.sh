#!/bin/bash
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"
REGION="${GCP_REGION:-us-central1}"
SERVICE_NAME="teachback"
IMAGE="gcr.io/$PROJECT_ID/$SERVICE_NAME"

echo "Building container..."
gcloud builds submit --tag "$IMAGE" --project "$PROJECT_ID"

echo "Deploying to Cloud Run..."
gcloud run deploy "$SERVICE_NAME" \
  --image "$IMAGE" \
  --platform managed \
  --region "$REGION" \
  --allow-unauthenticated \
  --set-env-vars "GOOGLE_API_KEY=${GOOGLE_API_KEY:-},PRESET_TRAILS_GCS_BUCKET=${PRESET_TRAILS_GCS_BUCKET:-},PRESET_TRAILS_GCS_PREFIX=${PRESET_TRAILS_GCS_PREFIX:-},PRESET_TRAILS_AUTO_SYNC=${PRESET_TRAILS_AUTO_SYNC:-false}" \
  --memory 1Gi \
  --cpu 2 \
  --timeout 900 \
  --max-instances 1 \
  --project "$PROJECT_ID"

echo "Deployed! URL:"
gcloud run services describe "$SERVICE_NAME" \
  --platform managed \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --format "value(status.url)"
