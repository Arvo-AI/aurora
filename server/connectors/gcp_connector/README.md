# GCP Connector

Workload Identity Federation (WIF) for secure, keyless cross-project access.
Also supports service account key upload and legacy OAuth.

---

## How It Works

Aurora uses its own GCP service account identity to federate into customer
projects via Workload Identity Federation. The customer creates a WIF pool
that trusts Aurora's identity, plus one or two service accounts Aurora can
impersonate. This is the GCP equivalent of AWS STS AssumeRole.

```text
Aurora's identity (SA key or GKE Workload Identity)
  └─> iamcredentials.generateAccessToken(customer's SA)
        └─> Short-lived token (1-hour, auto-refreshed)
```

---

## Prerequisites

| Requirement | Who Provides It |
|---|---|
| Aurora's OIDC issuer URL | Aurora operator (see Operator Setup below) |
| Aurora's SA email | Aurora operator (see Operator Setup below) |
| Terraform or gcloud CLI | Customer runs the setup module/script |

---

## Operator Setup (Aurora's Own GCP Identity)

Before users can connect their GCP projects, the Aurora operator must configure
Aurora's own GCP identity. This identity is used solely to call
`iamcredentials.generateAccessToken` on customer service accounts.

Choose **one** of the paths below depending on your deployment.

---

### Path A: Static SA Key (Docker Compose / non-GKE)

#### 1. Create a Service Account for Aurora

In a GCP project you control (e.g., `aurora-saas-prod`):

```bash
gcloud iam service-accounts create aurora-wif \
  --project=<YOUR_AURORA_PROJECT> \
  --display-name="Aurora WIF Identity" \
  --description="Identity used by Aurora to federate into customer projects"
```

This SA has **no roles in customer projects**. It is purely an identity.

#### 2. Create a Key

```bash
gcloud iam service-accounts keys create aurora-wif-key.json \
  --iam-account=aurora-wif@<YOUR_AURORA_PROJECT>.iam.gserviceaccount.com
```

Store this key file securely (e.g., mount it as a Docker secret).

#### 3. Configure `.env`

```bash
AURORA_WIF_CREDENTIAL_SOURCE=json_file
AURORA_WIF_SA_KEY_PATH=/path/to/aurora-wif-key.json
AURORA_WIF_SA_EMAIL=aurora-wif@<YOUR_AURORA_PROJECT>.iam.gserviceaccount.com
```

#### 4. Note the OIDC Issuer

For Google service accounts, the OIDC issuer is always:

```
https://accounts.google.com
```

Provide this URL and the SA email to customers when they run the setup module.

---

### Path B: GKE Workload Identity (Kubernetes on GKE)

When running on GKE with Workload Identity enabled, the pod's Kubernetes
service account is automatically mapped to a GCP service account. No key
file needed.

#### 1. Create the GCP Service Account

```bash
gcloud iam service-accounts create aurora-wif \
  --project=<YOUR_AURORA_PROJECT> \
  --display-name="Aurora WIF Identity"
```

#### 2. Bind the Kubernetes SA to the GCP SA

```bash
gcloud iam service-accounts add-iam-policy-binding \
  aurora-wif@<YOUR_AURORA_PROJECT>.iam.gserviceaccount.com \
  --role=roles/iam.workloadIdentityUser \
  --member="serviceAccount:<YOUR_AURORA_PROJECT>.svc.id.goog[<NAMESPACE>/<K8S_SA_NAME>]"
```

#### 3. Annotate the Kubernetes Service Account

```yaml
# In your Helm values or deployment spec
serviceAccount:
  annotations:
    iam.gke.io/gcp-service-account: aurora-wif@<YOUR_AURORA_PROJECT>.iam.gserviceaccount.com
```

#### 4. Configure `.env`

```bash
AURORA_WIF_CREDENTIAL_SOURCE=gke_metadata
AURORA_WIF_SA_EMAIL=aurora-wif@<YOUR_AURORA_PROJECT>.iam.gserviceaccount.com
```

#### 5. Note the OIDC Issuer

For GKE Workload Identity, the issuer is your cluster's OIDC URL:

```bash
gcloud container clusters describe <CLUSTER> --zone <ZONE> \
  --format='value(selfLink)'
```

Or use `https://accounts.google.com` (works for both paths).

---

## Connecting Customer GCP Projects

Once the operator has configured Aurora's identity, customers can connect
their GCP projects.

### Option A: Terraform Module (Recommended)

The Terraform module is in `server/connectors/gcp_connector/terraform/`.

```bash
cd server/connectors/gcp_connector/terraform/

terraform init
terraform apply \
  -var="project_id=<CUSTOMER_PROJECT>" \
  -var="aurora_oidc_issuer=https://accounts.google.com" \
  -var="aurora_sa_email=aurora-wif@<AURORA_PROJECT>.iam.gserviceaccount.com"
```

Copy the `wif_config` output and paste it into the Aurora GCP connection page.

### Option B: gcloud Script

```bash
bash server/connectors/gcp_connector/terraform/setup.sh \
  --project <CUSTOMER_PROJECT> \
  --aurora-issuer https://accounts.google.com \
  --aurora-sa aurora-wif@<AURORA_PROJECT>.iam.gserviceaccount.com
```

The script outputs the WIF config values to paste into Aurora.

### Option C: Service Account Key Upload

For air-gapped or restricted environments, upload a SA key JSON directly.
No WIF setup required. See the Service Account tab on the GCP auth page.

---

## What the Customer Setup Creates

| Resource | Purpose |
|---|---|
| `aurora-wif-pool` | Workload Identity Pool trusting Aurora's OIDC issuer |
| `aurora-provider` | OIDC provider with confused-deputy prevention (`assertion.sub == aurora-sa-email`) |
| `aurora-agent` SA | Full-access SA for Aurora's Agent mode (`roles/editor`) |
| `aurora-viewer` SA | Read-only SA for Aurora's Ask mode (`roles/viewer` + read-only roles) |
| Required API enablement | 27 APIs including STS, IAM credentials, Cloud Resource Manager |

---

## Legacy OAuth Setup

OAuth is still supported but not recommended for new deployments. It requires
Aurora to manage refresh tokens and provision per-user service accounts.

### 1. Create OAuth Credentials

1. Go to [GCP Console > Credentials](https://console.cloud.google.com/apis/credentials)
2. Configure **OAuth consent screen** (if first time):
   - User Type: External
   - App name: `Aurora`
   - Add your email as a test user
3. Click **+ CREATE CREDENTIALS** > **OAuth client ID**
   - Type: Web application
   - Redirect URI: `http://localhost:5000/callback`
4. Copy the **Client ID** and **Client Secret**

### 2. Configure `.env`

```bash
CLIENT_ID=your-client-id.apps.googleusercontent.com
CLIENT_SECRET=your-client-secret
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `AURORA_WIF_CREDENTIAL_SOURCE` | For WIF | `json_file` | `json_file` or `gke_metadata` |
| `AURORA_WIF_SA_KEY_PATH` | For `json_file` | — | Path to Aurora's SA key JSON |
| `AURORA_WIF_SA_EMAIL` | For WIF | — | Aurora's WIF service account email |
| `CLIENT_ID` | For OAuth | — | Google OAuth client ID |
| `CLIENT_SECRET` | For OAuth | — | Google OAuth client secret |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "WIF verification failed" on connect | WIF pool doesn't trust Aurora's SA | Verify `attribute_condition` in the WIF provider matches Aurora's SA email |
| "Permission denied" on generateAccessToken | Aurora's SA not authorized to impersonate customer SA | Check `roles/iam.workloadIdentityUser` binding on the customer SA |
| "AURORA_WIF_SA_KEY_PATH is not set" | Missing env var | Set `AURORA_WIF_SA_KEY_PATH` in `.env` pointing to the key file |
| Token exchange works but gcloud fails | Credential config file issue | Check that `GOOGLE_APPLICATION_CREDENTIALS` points to a valid `external_account` JSON |
| "No GCP projects found" after WIF connect | SA has no project access | Grant `roles/viewer` (or higher) to the customer's `aurora-agent` SA on the target project |
