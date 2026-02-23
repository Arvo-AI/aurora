# Leads and Accounts to Connection Analytics Design Doc

**TL;DR**: We need hard numbers on where the outbound pipeline stalls before messaging even matters. That means tracking the path from (lead source + lead metadata + assigned sender account) to (connection request sent) to (connection request accepted). The hard part is attribution: preserving upstream metadata when leads enter HeyReach, and joining it to connection outcomes. Do that first. The rest is just reporting.

---

## Customer

Internal, outbound operator, Ben

---

## Problem

- We do not know if the bottleneck is lead quality, list segmentation, sender account distribution, or LinkedIn connection acceptance
- HeyReach has dashboard metrics, but they are not tied to our upstream lead sources (Clay, HR, ZoomInfo, Search, etc.)
- We cannot answer basic questions:
  - Which lead source produces the highest connection acceptance rate?
  - Which keyword or ICP slice performs, and which is dead weight?
  - Which sender accounts are carrying volume and which are idle?
  - Are we failing at importing leads correctly, or failing at getting accepted?

---

## Solution & Exact delta

### Goal

One script that produces a complete report answering:

1. How many leads did we import this period (by source and segment)?
2. How many connection requests were sent (by source, segment, campaign, sender account)?
3. How many were accepted (and acceptance rate), plus time to acceptance
4. Where is the bottleneck (import failures, low acceptance segments, account-level issues)?

Scope boundary: this doc stops at **connections**. Messages, meetings, users are downstream and explicitly out of scope.

### Attribution (Core Problem)

If we lose lead metadata at import time, we cannot measure anything later. We need a single join key that survives the entire HeyReach lifecycle.

#### Data sources

```
UPSTREAM (Clay, HR, ZoomInfo, manual lists)     HEYREACH (execution and outcomes)
───────────────────────────────────────────     ───────────────────────────────

* Lead source                                   - Lead lists (lead and company)
* Query / keywords                              - Campaigns
* ICP tags (SRE, Platform, DevOps, etc.)        - LinkedIn sender accounts
* Company attributes (region, size, industry)   - Connection outcomes (sent, accepted)
* Batch id / run id                             - Stats (overall)
```

#### Canonical lead key

Use **LinkedIn profile URL** as the primary key. Normalize it aggressively (lowercase, strip params, normalize trailing slashes). This is the only identifier that is stable across tools.

#### Where attribution should live

Store upstream fields into HeyReach lead records using `customUserFields` at import time. This ensures the metadata is available later when we pull leads or ingest events.

Rules that matter:

- `customUserFields[].name` must be alpha-numeric or underscores only.
- If the name is invalid, the API errors and we lose attribution.
- Adding leads to a list is not the same as adding them to a campaign, so attribution must be attached at the point we actually import, not assumed later.

Recommended `customUserFields` (names must be underscore safe):

- `source` (clay, zoominfo, hr, manual)
- `source_list` (table or list name)
- `keywords` (short string)
- `icp` (sre, platform, devops, eng_manager, exec)
- `batch_id` (timestamped run id)
- `enrichment_level` (raw, enriched)
- `company_region` (na, eu, apac, unknown)
- `is_employed` (true, false, unknown) - scraped from LinkedIn profile
- `estimated_age` (integer) - inferred from university graduation date via LLM
- `gender` (male, female, unknown) - inferred from LinkedIn profile via LLM

### Observing Connection Outcomes

We need connection outcomes at the event level, not just a dashboard snapshot. All data will be pulled via HeyReach API batch analytics and ingestion.

#### Approach: API batch ingestion

Pull connection outcome data periodically via HeyReach API endpoints:

- Campaign progressStats (total users, in progress, pending, finished, failed)
- Stats endpoint for coarse totals and acceptance rates
- List endpoints to fetch leads with their current campaign status

Implementation detail:

- Scheduled batch job (nightly or weekly) pulls all campaigns, lists, and lead statuses
- Store outcomes in local DB keyed by normalized LinkedIn profile URL (plus campaign id and sender account id)
- Compute acceptance rate and time-to-accept from campaign progression data
- Join with upstream attribution via `customUserFields` to preserve segmentation

### Variables

All variables we track for this link. Anything not present in HeyReach must be injected at import time (custom fields) or in our own config.

#### Lead variables (attribution and quality)

| Variable | Source | How to compute |
|---|---|---|
| `linkedin_profile_url` | Upstream and HeyReach | Canonical join key (normalized) |
| `source` | Upstream | Inject into `customUserFields.source` |
| `source_list` | Upstream | Inject into `customUserFields.source_list` |
| `keywords` | Upstream | Inject into `customUserFields.keywords` |
| `icp` | Upstream | Inject into `customUserFields.icp` |
| `company_name` | Upstream or HeyReach | Prefer upstream, else HeyReach lead fields if present |
| `region` | Upstream | Inject into `customUserFields.company_region` |
| `title` | Upstream | Store upstream value, and optionally mirror into custom field if HeyReach lead model does not store it reliably |
| `is_employed` | LinkedIn scrape + LLM | Scrape profile, infer employment status via LLM, store in `customUserFields.is_employed` |
| `estimated_age` | LinkedIn scrape + LLM | Scrape education section, infer graduation year, compute age via LLM, store in `customUserFields.estimated_age` |
| `gender` | LinkedIn scrape + LLM | Scrape profile (name, photo if available), infer gender via LLM, store in `customUserFields.gender` |
| `is_valid_profile_url` | Derived | URL parse and normalize succeeded |
| `is_duplicate` | Derived | Same normalized URL appears multiple times in a batch |

#### Account variables (sender account performance)

| Variable | Source | How to compute |
|---|---|---|
| `account_id` | HeyReach | From LinkedIn accounts list |
| `account_label` | Internal config | Map account_id to human name (Ben, Mohamed, etc.) |
| `account_type` | Internal config | real_personal, ai_mirror_saas, ai_aimfox_saas |
| `account_gender` | Internal config | male, female, neutral (for AI accounts) |
| `account_job_title` | Internal config | Job/role displayed on the LinkedIn account (SRE, DevOps Engineer, CTO, etc.) |
| `account_location` | Internal config | Geographic location of the account |
| `account_seniority` | Internal config | junior, mid, senior, exec (for targeting matching) |
| `account_industry` | Internal config | tech, finance, healthcare, etc. |
| `connection_sent` | HeyReach API | Count from campaign progressStats and lead statuses |
| `connection_accepted` | HeyReach API | Count from campaign progressStats and lead statuses |
| `accept_rate` | Derived | accepted / sent |
| `accept_rate_by_lead_gender` | Derived | Acceptance rate segmented by lead gender (to analyze account-lead gender matching) |
| `accept_rate_by_lead_seniority` | Derived | Acceptance rate segmented by lead ICP/seniority |
| `daily_volume` | Derived | sent per day per account |
| `idle_days` | Derived | days with zero sends (in the period) |
| `account_age_days` | Internal config | Days since account was added to rotation |
| `connection_limit_hits` | HeyReach API | Number of times account hit LinkedIn connection limits |

#### Connection outcome variables (the point)

| Variable | Source | How to compute |
|---|---|---|
| `connection_request_sent_at` | HeyReach API | Timestamp from campaign progression or lead status |
| `connection_request_accepted_at` | HeyReach API | Timestamp from campaign progression or lead status |
| `time_to_accept_hours` | Derived | accepted_at - sent_at |
| `status` | Derived | pending if sent and not accepted within window (ex: 14 days) |

#### Operational variables (import and plumbing)

| Variable | Source | How to compute |
|---|---|---|
| `leads_imported` | API response | Added + updated counts from import calls |
| `leads_failed_import` | API response | Failed count from import calls |
| `failure_reasons` | Logs | Capture API errors (invalid custom field names, missing required fields, etc.) |

### Report Structure

Markdown output sections:

1. Summary totals (leads imported, sent, accepted, accept rate)
2. Funnel (imported -> sent -> accepted) with conversion rates
3. By Source (Clay vs ZoomInfo vs HR vs manual)
4. By ICP slice (SRE vs Platform vs DevOps vs Exec)
5. By Keywords (top 20 by volume, and worst 20 by accept rate)
6. By Sender Account (volume, accept rate, idle days)
7. By Account Type (real_personal vs ai_mirror_saas vs ai_aimfox_saas performance comparison)
8. By Account Gender (acceptance rates segmented by account gender)
9. By Account Job Title (which job titles perform best for connection acceptance)
10. Cross-Analysis: Account-Lead Matching
    - Account gender vs lead gender (do same-gender connections perform better?)
    - Account seniority vs lead seniority (do peer-level connections work better?)
    - Account industry vs lead industry alignment
11. By Campaign (progressStats snapshot plus event-based sent/accepted)
12. Time to Accept (median, p75, p90)
13. Bottleneck diagnosis
    - Import failures (counts and reasons)
    - Segments with high volume and low acceptance
    - Accounts with low send volume or low accept rate
    - AI vs real account performance gaps

### What's Done

- We already have HeyReach API access patterns and clients (campaigns, lists, accounts) from the existing HeyReach integration code.
- HeyReach supports campaign APIs, lists APIs, LinkedIn account APIs, and import APIs that support account-to-lead mapping.

### What's Left

- Implement LinkedIn profile scraper with rate limiting and proxy rotation
- Build LLM inference pipeline for employment status, age, and gender extraction (for leads)
- Create account metadata configuration file mapping account_id to type, gender, job_title, seniority, industry, location
- Add scheduled batch job (nightly or weekly) for analytics script
- Validate attribution coverage:
  - If `customUserFields` are missing on a big fraction of leads, upstream ingestion is broken
  - If account metadata is incomplete, account-level analysis will be limited

### PR Structure

#### PR #1: DB schema + account metadata system

- Implement all four database tables (`heyreach_leads`, `heyreach_accounts`, `heyreach_connections`, `heyreach_analytics_snapshots`)
- Create account metadata configuration system with YAML loader and validation
- Build data ingestion pipeline to populate accounts from config file

#### PR #2: Lead enrichment pipeline (LinkedIn scraping + LLM inference)

- Build LinkedIn profile scraper with rate limiting, proxy rotation, and HTML parsing
- Implement LLM inference for employment status, estimated age (from graduation year), and gender
- Store enriched data in `heyreach_leads` table with enrichment status tracking

#### PR #3: Connection analytics + continuous analysis

- Batch ingestion of HeyReach API data (campaigns, lists, leads, progressStats) into DB tables
- Join enrichment data with connection outcomes across all defined categories
- Compute all metrics and segmentation breakdowns
- Generate markdown + JSON reports with full analysis
- Implement scheduled jobs (daily batch, weekly deep analysis) with alerting

---

## PFP (Potential Failure Points)

### 1. Attribution gets dropped at import time

If custom fields are not written on import, the report becomes useless. You will see totals, but no segmentation.

**Mitigation:** enforce custom fields in the importer and fail the batch if required attribution keys are missing.

### 2. LinkedIn scraping fails or gets rate limited

If LinkedIn blocks scraping or profile pages become inaccessible, enrichment data will be incomplete and segmentation analysis will be limited.

**Mitigation:** use rotating proxies, implement exponential backoff, and cache scraped profiles. Mark leads as "enrichment pending" vs "enrichment failed" to track coverage.

### 3. URL normalization bugs create false duplicates

Different URL formats for the same profile will blow up joins.

**Mitigation:** normalize aggressively and write a unit-tested normalizer. Store both raw and normalized.

### 4. Account mapping is wrong

If leads are not mapped to the correct sender account, account-level performance analysis is garbage.

**Mitigation:** always use AddLeadsToCampaignV2 accountLeadPairs mapping when distributing leads across accounts.

### 5. Account metadata is incomplete or incorrect

If account type, gender, job_title, or other metadata is missing or wrong, account-level segmentation and matching analysis will be meaningless.

**Mitigation:** maintain a single source of truth config file (YAML or JSON) for account metadata. Validate on startup and fail if critical fields are missing for active accounts.

---

## Technical implementation

### 1. Define categories and important variables to analyze

**Lead categories:**
- Source (clay, zoominfo, hr, manual)
- ICP (sre, platform, devops, eng_manager, exec)
- Keywords (segmented by search terms)
- Geography (na, eu, apac)
- Employment status (employed, not_employed, unknown)
- Age brackets (20-25, 26-30, 31-35, 36-40, 40+)
- Gender (male, female, unknown)

**Account categories:**
- Type (real_personal, ai_mirror_saas, ai_aimfox_saas)
- Gender (male, female, neutral)
- Job title (SRE, DevOps Engineer, Platform Engineer, Cloud Architect, etc.)
- Seniority (junior, mid, senior, exec)
- Industry (tech, finance, healthcare, etc.)

**Connection outcome categories:**
- Status (sent, accepted, rejected, pending, expired)
- Time to accept brackets (0-24h, 24-48h, 48-72h, 3-7d, 7-14d, 14d+)

**Important variables to analyze:**
- Connection acceptance rate by lead source
- Connection acceptance rate by lead ICP
- Connection acceptance rate by account type (real vs AI)
- Connection acceptance rate by account gender
- Connection acceptance rate by account-lead gender match (same vs different)
- Connection acceptance rate by account-lead seniority match
- Daily volume per account
- Time to accept distribution
- Import failure rate by source
- Idle account detection (accounts not sending)

### 2. Define DB schema for the segment

#### Table: `heyreach_leads`

```sql
CREATE TABLE heyreach_leads (
    id SERIAL PRIMARY KEY,
    linkedin_profile_url VARCHAR(500) UNIQUE NOT NULL,
    normalized_profile_url VARCHAR(500) NOT NULL,
    
    -- Attribution
    source VARCHAR(50),
    source_list VARCHAR(200),
    keywords VARCHAR(500),
    icp VARCHAR(50),
    batch_id VARCHAR(100),
    
    -- Company
    company_name VARCHAR(200),
    company_region VARCHAR(50),
    company_size VARCHAR(50),
    company_industry VARCHAR(100),
    
    -- Lead details
    title VARCHAR(200),
    
    -- Enriched data (from LinkedIn scraping + LLM)
    is_employed BOOLEAN,
    estimated_age INTEGER,
    gender VARCHAR(20),
    enrichment_timestamp TIMESTAMP,
    enrichment_status VARCHAR(50), -- pending, completed, failed
    
    -- Metadata
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    
    INDEX idx_normalized_url (normalized_profile_url),
    INDEX idx_source (source),
    INDEX idx_icp (icp),
    INDEX idx_batch_id (batch_id)
);
```

#### Table: `heyreach_accounts`

```sql
CREATE TABLE heyreach_accounts (
    id SERIAL PRIMARY KEY,
    account_id VARCHAR(100) UNIQUE NOT NULL,
    label VARCHAR(100) NOT NULL,
    
    -- Account metadata
    account_type VARCHAR(50) NOT NULL, -- real_personal, ai_mirror_saas, ai_aimfox_saas
    gender VARCHAR(20),
    job_title VARCHAR(200),
    seniority VARCHAR(50),
    industry VARCHAR(100),
    location VARCHAR(200),
    
    -- Tracking
    account_age_days INTEGER,
    is_active BOOLEAN DEFAULT TRUE,
    
    -- Metadata
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    
    INDEX idx_account_id (account_id),
    INDEX idx_account_type (account_type),
    INDEX idx_is_active (is_active)
);
```

#### Table: `heyreach_connections`

```sql
CREATE TABLE heyreach_connections (
    id SERIAL PRIMARY KEY,
    
    -- Join keys
    lead_id INTEGER REFERENCES heyreach_leads(id),
    account_id INTEGER REFERENCES heyreach_accounts(id),
    campaign_id VARCHAR(100),
    list_id VARCHAR(100),
    
    -- Connection outcome
    status VARCHAR(50) NOT NULL, -- sent, accepted, rejected, pending, expired
    connection_sent_at TIMESTAMP,
    connection_accepted_at TIMESTAMP,
    connection_rejected_at TIMESTAMP,
    time_to_accept_hours DECIMAL(10, 2),
    
    -- Metadata
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    
    INDEX idx_lead_id (lead_id),
    INDEX idx_account_id (account_id),
    INDEX idx_campaign_id (campaign_id),
    INDEX idx_status (status),
    INDEX idx_sent_at (connection_sent_at),
    INDEX idx_accepted_at (connection_accepted_at),
    
    UNIQUE (lead_id, account_id, campaign_id)
);
```

#### Table: `heyreach_analytics_snapshots`

```sql
CREATE TABLE heyreach_analytics_snapshots (
    id SERIAL PRIMARY KEY,
    snapshot_date DATE NOT NULL,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    
    -- Aggregated metrics (stored as JSONB for flexibility)
    metrics JSONB NOT NULL,
    
    -- Metadata
    created_at TIMESTAMP DEFAULT NOW(),
    
    INDEX idx_snapshot_date (snapshot_date),
    INDEX idx_period (period_start, period_end)
);
```

### 3. Continuous analysis

**Schedule:**
- Daily batch job (runs at 2 AM UTC)
- Weekly deep analysis (runs Sunday 3 AM UTC)
- On-demand via CLI for custom date ranges

**Daily batch job workflow:**
1. Fetch new leads from HeyReach API (last 24 hours)
2. Enrich leads without enrichment data (LinkedIn scraping + LLM inference)
3. Fetch connection outcomes from HeyReach API
4. Update `heyreach_connections` table with latest statuses
5. Compute daily metrics and store in `heyreach_analytics_snapshots`
6. Generate lightweight daily report (JSON only, no markdown)

**Weekly deep analysis workflow:**
1. Run full batch ingestion (last 7 days)
2. Compute all segmentation metrics
3. Generate full markdown report with all breakdowns
4. Send report via email or Slack
5. Alert on anomalies (sudden acceptance rate drop, idle accounts, etc.)

**On-demand CLI:**
```bash
python scripts/analytics/connection_analytics.py \
  --since 2026-01-01 \
  --until 2026-02-01 \
  --output-dir data/reports \
  --format markdown,json
```

**Continuous improvement loop:**
- Track acceptance rate trends over time
- A/B test different account types on same lead segments
- Identify underperforming segments and pause them
- Identify high-performing account-lead combinations and scale them
- Monitor enrichment data quality (% of leads with complete enrichment)

### Workflow

```
┌──────────────────────────────────────────────────────────────┐
│ RUN: python scripts/analytics/connection_analytics.py         │
│      --since 2026-01-01                                       │
├──────────────────────────────────────────────────────────────┤
│ 1. FETCH SENDER ACCOUNTS                                      │
│    PublicLinkedInAccount.GetAll                                │
│    Build account_id -> label map                               │
│                                                              │
│ 2. FETCH CAMPAIGNS                                             │
│    PublicCampaigns.GetAll                                      │
│    Capture campaignAccountIds and progressStats                │
│                                                              │
│ 3. FETCH LISTS                                                 │
│    PublicList.GetAll                                           │
│    Map list_id -> list_name                                    │
│                                                              │
│ 4. FETCH LEADS FOR SEGMENTS                                    │
│    PublicList.GetLeadsFromList / GetCompaniesFromList          │
│    Pull customUserFields to preserve attribution               │
│                                                              │
│ 5. SCRAPE & ENRICH LEAD PROFILES                               │
│    For each lead without enrichment data:                      │
│      - Scrape LinkedIn profile page                            │
│      - Feed scraped content to LLM                             │
│      - Infer: is_employed, estimated_age, gender               │
│      - Store back to HeyReach customUserFields                 │
│                                                              │
│ 6. INGEST CONNECTION OUTCOMES (API batch pull)                 │
│    Pull campaign progressStats and lead statuses               │
│    Extract connection_request_sent, connection_request_accepted│
│                                                              │
│ 7. JOIN + COMPUTE METRICS                                      │
│    Normalize profile URLs                                      │
│    sent, accepted, accept_rate                                 │
│    time_to_accept distributions                                │
│    breakdowns by source, icp, keywords, list, campaign, account│
│    account performance by type (real vs AI), gender, job title │
│    cross-analysis: account-lead gender matching effects        │
│                                                              │
│ 8. BUILD REPORT                                                │
│    Funnel and bottlenecks                                      │
│    Outliers and dead segments                                  │
│                                                              │
│ OUTPUT:                                                       │
│  data/processed/connection_analytics_YYYY-MM-DDTHHMMSS.json    │
│  data/processed/connection_analytics_YYYY-MM-DDTHHMMSS.md      │
└──────────────────────────────────────────────────────────────┘
```

### API mechanics that matter

- Authentication is via `X-API-KEY` header.
- Rate limit is 60 requests per minute, and exceeding returns HTTP 429.
- Campaign fetch includes progressStats counts and campaignAccountIds, which are useful for coarse monitoring.
- List endpoints support pulling up to 1000 leads per request, so pagination is required for real volumes.
- Add-to-list and add-to-campaign are different operations. Do not assume a list import automatically starts a campaign.

### LinkedIn profile scraping and LLM enrichment

Lead enrichment happens via LinkedIn profile scraping followed by LLM inference:

- Scrape each lead's LinkedIn profile page (respect rate limits, use rotating proxies if needed)
- Extract raw HTML or structured sections (employment, education, about)
- Feed scraped content to LLM with structured prompt for inference
- LLM returns: `is_employed` (boolean), `estimated_age` (integer based on graduation year), `gender` (male/female/unknown)
- Store inferred values back to HeyReach lead via `customUserFields` update
- Track enrichment timestamp to avoid re-scraping recently processed profiles

### Configuration

```bash
HEYREACH_API_KEY=...

# LinkedIn scraping
LINKEDIN_SCRAPE_RATE_LIMIT=10  # requests per minute
LINKEDIN_PROXY_POOL=...         # comma-separated proxy URLs

# LLM inference
LLM_MODEL=gpt-4
LLM_API_KEY=...

# Account metadata
ACCOUNT_METADATA_PATH=config/heyreach_accounts.yaml

# Analytics
DEFAULT_LOOKBACK_DAYS=30
```

### Account metadata configuration

Create `config/heyreach_accounts.yaml`:

```yaml
accounts:
  - account_id: "abc123"
    label: "Ben"
    type: "real_personal"
    gender: "male"
    job_title: "SRE Manager"
    seniority: "senior"
    industry: "tech"
    location: "San Francisco, CA"
    
  - account_id: "def456"
    label: "Mohamed"
    type: "real_personal"
    gender: "male"
    job_title: "DevOps Engineer"
    seniority: "mid"
    industry: "tech"
    location: "New York, NY"
    
  - account_id: "ghi789"
    label: "AI Sarah (Mirror)"
    type: "ai_mirror_saas"
    gender: "female"
    job_title: "Platform Engineer"
    seniority: "mid"
    industry: "tech"
    location: "Remote"
    
  - account_id: "jkl012"
    label: "AI John (AimFox)"
    type: "ai_aimfox_saas"
    gender: "male"
    job_title: "Cloud Architect"
    seniority: "senior"
    industry: "tech"
    location: "Remote"
```
