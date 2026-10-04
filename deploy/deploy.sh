#!/usr/bin/env bash
# Sets up Hello Daylight in a NEW Google Cloud project: one image, a web service, a nightly job, a scheduler.
# Nothing here is specific to one operator. Read it once, then run it with your own values:
#
#   PROJECT_ID=my-daylight REGION=europe-west3 ADMIN_EMAIL=me@example.com \
#   BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX deploy/deploy.sh
#
# What it costs when nobody uses it: nothing. The web service scales to zero, there is no minimum instance, the job
# exists only while it runs, Firestore and Secret Manager bill by use. What it may cost when used: see the README.
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID (a new project, not one you use for something else)}"
: "${ADMIN_EMAIL:?set ADMIN_EMAIL (the operator who may enter and open the admin page)}"
REGION="${REGION:-europe-west3}"
TIMEZONE="${TIMEZONE:-America/Toronto}"
NIGHT_CRON="${NIGHT_CRON:-0 23 * * *}"
SERVICE=daylight-web
JOB=daylight-night
REPO=daylight
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/app:$(git rev-parse --short HEAD 2>/dev/null || date +%s)"
RUN_SA="daylight-run@${PROJECT_ID}.iam.gserviceaccount.com"
SCHED_SA="daylight-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"

echo "==> project ${PROJECT_ID}, region ${REGION}. This changes billing-relevant settings. Ctrl-C now to stop."
sleep 5
gcloud config set project "${PROJECT_ID}" >/dev/null

echo "==> APIs"
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com firestore.googleapis.com secretmanager.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com identitytoolkit.googleapis.com firebase.googleapis.com billingbudgets.googleapis.com

echo "==> Firestore (Native) and the rules that deny every browser"
gcloud firestore databases create --location="${REGION}" --type=firestore-native 2>/dev/null || echo "   (database exists)"

echo "==> service accounts"
gcloud iam service-accounts create daylight-run --display-name="Hello Daylight runtime" 2>/dev/null || true
gcloud iam service-accounts create daylight-scheduler --display-name="Hello Daylight scheduler" 2>/dev/null || true
for role in roles/datastore.user roles/secretmanager.secretAccessor roles/run.developer; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" --member="serviceAccount:${RUN_SA}" --role="${role}" --condition=None >/dev/null
done
gcloud projects add-iam-policy-binding "${PROJECT_ID}" --member="serviceAccount:${SCHED_SA}" --role=roles/run.invoker --condition=None >/dev/null

echo "==> secrets (you are asked for the Gemini key; the session secret is generated)"
if ! gcloud secrets describe daylight-gemini-key >/dev/null 2>&1; then
  read -r -s -p "Gemini API key for signed-in runs: " GEMINI_KEY; echo
  printf '%s' "${GEMINI_KEY}" | gcloud secrets create daylight-gemini-key --data-file=-
fi
if ! gcloud secrets describe daylight-session-secret >/dev/null 2>&1; then
  python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")' | gcloud secrets create daylight-session-secret --data-file=-
fi

echo "==> build the image"
gcloud artifacts repositories create "${REPO}" --repository-format=docker --location="${REGION}" 2>/dev/null || true
gcloud builds submit --tag "${IMAGE}" .

# The three Firebase web values are public by design. Create the web app in the Firebase console, enable Google sign-in,
# then export FIREBASE_WEB_API_KEY and FIREBASE_AUTH_DOMAIN before running this script.
: "${FIREBASE_WEB_API_KEY:?export FIREBASE_WEB_API_KEY (Firebase console > project settings > web app)}"
FIREBASE_AUTH_DOMAIN="${FIREBASE_AUTH_DOMAIN:-${PROJECT_ID}.firebaseapp.com}"
JOB_NAME="projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB}"
COMMON_ENV="DAYLIGHT_BACKEND=firestore,DAYLIGHT_AUTH_MODE=firebase,DAYLIGHT_ADMIN_EMAILS=${ADMIN_EMAIL},FIREBASE_PROJECT_ID=${PROJECT_ID},FIREBASE_WEB_API_KEY=${FIREBASE_WEB_API_KEY},FIREBASE_AUTH_DOMAIN=${FIREBASE_AUTH_DOMAIN},GOOGLE_CLOUD_PROJECT=${PROJECT_ID}"
COMMON_SECRETS="GEMINI_API_KEY=daylight-gemini-key:latest,DAYLIGHT_SESSION_SECRET=daylight-session-secret:latest"

echo "==> the nightly job (no retries of its own: a dead run is resumed by the next start, never paid twice)"
gcloud run jobs deploy "${JOB}" --image="${IMAGE}" --region="${REGION}" --service-account="${RUN_SA}" \
  --command=python --args="-m,app.night,--wait-stale" --max-retries=1 --task-timeout=1800s --cpu=1 --memory=1Gi \
  --set-env-vars="${COMMON_ENV}" --set-secrets="${COMMON_SECRETS}"

echo "==> the web service (scales to zero, no minimum instance, two instances at most)"
gcloud run deploy "${SERVICE}" --image="${IMAGE}" --region="${REGION}" --service-account="${RUN_SA}" --allow-unauthenticated \
  --min-instances=0 --max-instances=2 --cpu=1 --memory=512Mi --timeout=900 --concurrency=20 \
  --set-env-vars="${COMMON_ENV},DAYLIGHT_LAUNCHER=cloudrun,DAYLIGHT_CLOUD_RUN_JOB=${JOB_NAME}" --set-secrets="${COMMON_SECRETS}"

echo "==> the schedule: ${NIGHT_CRON} (${TIMEZONE})"
gcloud scheduler jobs create http daylight-night --location="${REGION}" --schedule="${NIGHT_CRON}" --time-zone="${TIMEZONE}" \
  --uri="https://run.googleapis.com/v2/${JOB_NAME}:run" --http-method=POST --oauth-service-account-email="${SCHED_SA}" \
  --attempt-deadline=180s 2>/dev/null || gcloud scheduler jobs update http daylight-night --location="${REGION}" --schedule="${NIGHT_CRON}" --time-zone="${TIMEZONE}"

if [ -n "${BILLING_ACCOUNT:-}" ]; then
  echo "==> budget alarm at 20 EUR a month (an alarm, not a stop: the real stop is the run budget and the monthly caps in the app)"
  gcloud billing budgets create --billing-account="${BILLING_ACCOUNT}" --display-name="daylight ${PROJECT_ID}" \
    --budget-amount=20EUR --threshold-rule=percent=0.5 --threshold-rule=percent=1.0 \
    --filter-projects="projects/${PROJECT_ID}" 2>/dev/null || echo "   (budget exists or could not be created: check the console)"
fi

URL="$(gcloud run services describe "${SERVICE}" --region="${REGION}" --format='value(status.url)')"
echo
echo "Done. Open ${URL}"
echo "Last step by hand: Firebase console > Authentication > Settings > Authorized domains: add $(echo "${URL}" | sed 's#https://##')"
