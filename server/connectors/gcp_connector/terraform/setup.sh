#!/usr/bin/env bash
# Aurora GCP Workload Identity Federation setup script.
#
# Run this in a shell authenticated as a project Owner:
#   bash setup.sh \
#     --project my-project-id \
#     --aurora-sa aurora-wif@aurora-saas-prod.iam.gserviceaccount.com
#
# The script outputs the WIF config values to paste into Aurora.

set -euo pipefail

# ---- defaults ----
POOL_ID="aurora-wif-pool"
PROVIDER_ID="aurora-provider"
ENABLE_VIEWER=true
ADDITIONAL_PROJECTS=()
ORG_ID=""

usage() {
  cat <<EOF
Usage: $0 --project PROJECT_ID --aurora-sa SA_EMAIL [options]

Required:
  --project          GCP project ID
  --aurora-sa        Aurora's service account email (shown on the Aurora GCP connection page)

Options:
  --org-id ORG_ID             GCP organization ID (grants access to all org projects)
  --additional-project PID   Additional project Aurora should access (repeatable)
  --no-viewer                Skip creating the read-only viewer SA
  -h, --help                 Show this help
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)            PROJECT_ID="$2";        shift 2;;
    --aurora-sa)          AURORA_SA="$2";           shift 2;;
    --org-id)             ORG_ID="$2";              shift 2;;
    --additional-project) ADDITIONAL_PROJECTS+=("$2"); shift 2;;
    --no-viewer)          ENABLE_VIEWER=false;     shift;;
    -h|--help)            usage;;
    *) echo "Unknown option: $1"; usage;;
  esac
done

: "${PROJECT_ID:?--project is required}"
: "${AURORA_SA:?--aurora-sa is required}"

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

echo "Setting up Aurora WIF for project $PROJECT_ID ($PROJECT_NUMBER)"

# ---- Enable required APIs ----
APIS=(
  sts.googleapis.com iamcredentials.googleapis.com iam.googleapis.com
  cloudresourcemanager.googleapis.com compute.googleapis.com container.googleapis.com
  storage.googleapis.com monitoring.googleapis.com logging.googleapis.com
  bigquery.googleapis.com run.googleapis.com cloudbuild.googleapis.com
  artifactregistry.googleapis.com cloudasset.googleapis.com
)
echo "Enabling APIs..."
gcloud services enable "${APIS[@]}" --project="$PROJECT_ID" --quiet

# ---- WIF pool + provider ----
# Issuer is accounts.google.com because Aurora authenticates with a
# Google-signed ID token from its own GCP service account.
echo "Creating WIF pool..."
gcloud iam workload-identity-pools create "$POOL_ID" \
  --project="$PROJECT_ID" --location=global \
  --display-name="Aurora WIF Pool" \
  --description="Allows Aurora SaaS to federate into this project" \
  2>&1 || echo "  (pool may already exist)"

echo "Creating WIF provider..."
gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_ID" \
  --project="$PROJECT_ID" --location=global \
  --workload-identity-pool="$POOL_ID" \
  --issuer-uri="https://accounts.google.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.email=assertion.email" \
  --attribute-condition="attribute.email == \"$AURORA_SA\"" \
  2>&1 || echo "  (provider may already exist)"

# ---- Agent SA ----
AGENT_SA="aurora-agent@${PROJECT_ID}.iam.gserviceaccount.com"
echo "Creating agent SA..."
gcloud iam service-accounts create aurora-agent \
  --project="$PROJECT_ID" \
  --display-name="Aurora Agent" \
  --description="Full-access SA for Aurora Agent mode" \
  2>&1 || echo "  (SA may already exist)"

POOL_RESOURCE="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}"
gcloud iam service-accounts add-iam-policy-binding "$AGENT_SA" \
  --project="$PROJECT_ID" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/${POOL_RESOURCE}/*" \
  --condition=None --quiet

AGENT_ROLES=(roles/editor roles/iam.serviceAccountUser roles/bigquery.dataViewer)
if [[ -n "$ORG_ID" ]]; then
  echo "Granting org-level IAM roles (org: $ORG_ID)..."
  for role in "${AGENT_ROLES[@]}" roles/resourcemanager.organizationViewer; do
    gcloud organizations add-iam-policy-binding "$ORG_ID" \
      --member="serviceAccount:${AGENT_SA}" \
      --role="$role" --condition=None --quiet 2>/dev/null || true
  done
else
  ALL_PROJECTS=("$PROJECT_ID" "${ADDITIONAL_PROJECTS[@]}")
  for pid in "${ALL_PROJECTS[@]}"; do
    for role in "${AGENT_ROLES[@]}"; do
      gcloud projects add-iam-policy-binding "$pid" \
        --member="serviceAccount:${AGENT_SA}" \
        --role="$role" --condition=None --quiet 2>/dev/null || true
    done
  done
fi

# ---- Viewer SA ----
VIEWER_SA=""
if $ENABLE_VIEWER; then
  VIEWER_SA="aurora-viewer@${PROJECT_ID}.iam.gserviceaccount.com"
  echo "Creating viewer SA..."
  gcloud iam service-accounts create aurora-viewer \
    --project="$PROJECT_ID" \
    --display-name="Aurora Viewer" \
    --description="Read-only SA for Aurora Ask mode" \
    2>&1 || echo "  (SA may already exist)"

  gcloud iam service-accounts add-iam-policy-binding "$VIEWER_SA" \
    --project="$PROJECT_ID" \
    --role="roles/iam.workloadIdentityUser" \
    --member="principalSet://iam.googleapis.com/${POOL_RESOURCE}/*" \
    --condition=None --quiet

  VIEWER_ROLES=(
    roles/viewer roles/logging.viewer roles/monitoring.viewer
    roles/browser roles/cloudasset.viewer roles/compute.viewer
    roles/container.viewer roles/storage.objectViewer
  )
  if [[ -n "$ORG_ID" ]]; then
    for role in "${VIEWER_ROLES[@]}"; do
      gcloud organizations add-iam-policy-binding "$ORG_ID" \
        --member="serviceAccount:${VIEWER_SA}" \
        --role="$role" --condition=None --quiet 2>/dev/null || true
    done
  else
    for pid in "${ALL_PROJECTS[@]}"; do
      for role in "${VIEWER_ROLES[@]}"; do
        gcloud projects add-iam-policy-binding "$pid" \
          --member="serviceAccount:${VIEWER_SA}" \
          --role="$role" --condition=None --quiet 2>/dev/null || true
      done
    done
  fi
fi

# ---- Output ----
cat <<EOF

============================================================
Aurora WIF Configuration (paste into Aurora)
============================================================
  project_id:      $PROJECT_ID
  project_number:  $PROJECT_NUMBER
  pool_id:         $POOL_ID
  provider_id:     $PROVIDER_ID
  sa_email:        $AGENT_SA
  viewer_sa_email: ${VIEWER_SA:-(none)}
  org_id:          ${ORG_ID:-(none)}
============================================================
EOF
