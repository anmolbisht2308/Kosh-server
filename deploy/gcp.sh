#!/usr/bin/env bash
# Deploy the Kosh API to Google Cloud Run (Mumbai). Safe to re-run: it creates what's missing and redeploys.
#
#   PROJECT_ID=kosh-prod MONGO_URI='mongodb+srv://.../kosh' WEB_ORIGIN=https://kosh.example.com ./deploy/gcp.sh
#
# Private values can live in deploy/.env.deploy (git-ignored). Nothing here costs money beyond Cloud Run's
# free tier at small scale: no paid APIs, no SMS, no payment gateway. Prices come from free AMFI and NSE files.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f deploy/.env.deploy ]; then set -a; . deploy/.env.deploy; set +a; fi

: "${PROJECT_ID:?Set PROJECT_ID (your GCP project ID)}"
REGION="${REGION:-asia-south1}"
SERVICE="${SERVICE:-kosh-api}"
WEB_ORIGIN="${WEB_ORIGIN:-http://localhost:3300}"
RUN_SA="${SERVICE}@${PROJECT_ID}.iam.gserviceaccount.com"

step() { printf '\n\033[1;33m▶ %s\033[0m\n' "$*"; }

gcloud config set project "$PROJECT_ID" >/dev/null
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
BUILD_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

step "Enabling APIs (first run takes a minute)"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com cloudscheduler.googleapis.com

step "Service account the API runs as"
gcloud iam service-accounts describe "$RUN_SA" >/dev/null 2>&1 || gcloud iam service-accounts create "$SERVICE" --display-name "Kosh API (Cloud Run)"
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$BUILD_SA" --role roles/run.builder --condition None >/dev/null

step "Secrets"
secret() { # name value [update]
  if gcloud secrets describe "$1" >/dev/null 2>&1; then
    [ "${3:-}" = update ] && printf %s "$2" | gcloud secrets versions add "$1" --data-file=- >/dev/null
  else
    printf %s "$2" | gcloud secrets create "$1" --data-file=- --replication-policy automatic >/dev/null
  fi
  gcloud secrets add-iam-policy-binding "$1" --member "serviceAccount:$RUN_SA" --role roles/secretmanager.secretAccessor >/dev/null
}
if [ -n "${MONGO_URI:-}" ]; then secret kosh-mongo-uri "$MONGO_URI" update
elif ! gcloud secrets describe kosh-mongo-uri >/dev/null 2>&1; then echo "Set MONGO_URI on the first run"; exit 1; fi
secret kosh-jwt-secret "$(openssl rand -hex 32)"
secret kosh-jobs-secret "$(openssl rand -hex 24)"

step "Building and deploying $SERVICE to Cloud Run ($REGION)"
gcloud run deploy "$SERVICE" --source . --region "$REGION" --allow-unauthenticated \
  --service-account "$RUN_SA" \
  --set-env-vars "^@^ENV=production@WEB_ORIGIN=$WEB_ORIGIN" \
  --set-secrets "MONGO_URI=kosh-mongo-uri:latest,JWT_SECRET=kosh-jwt-secret:latest,JOBS_SECRET=kosh-jobs-secret:latest" \
  --memory 1Gi --cpu 1 --min-instances 0 --max-instances 2 --concurrency 20 --timeout 120

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"

step "Scheduler: refresh AMFI + NSE prices every 4 hours"
JOBS_SECRET="$(gcloud secrets versions access latest --secret kosh-jobs-secret)"
JOB_ARGS=(--location "$REGION" --schedule "15 */4 * * *" --time-zone "Asia/Kolkata" --uri "$URL/internal/refresh-prices" --http-method POST --attempt-deadline 300s)
if gcloud scheduler jobs describe kosh-prices --location "$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http kosh-prices "${JOB_ARGS[@]}" --update-headers "x-jobs-secret=$JOBS_SECRET" >/dev/null
else
  gcloud scheduler jobs create http kosh-prices "${JOB_ARGS[@]}" --headers "x-jobs-secret=$JOBS_SECRET" >/dev/null
fi

step "Health check"
curl -fsS "$URL/health" && echo
printf '\n\033[1;32m✓ API live at %s\033[0m\n' "$URL"
echo "Frontend: set NEXT_PUBLIC_API_URL=$URL on Netlify, and re-run this with WEB_ORIGIN=<frontend URL>."
