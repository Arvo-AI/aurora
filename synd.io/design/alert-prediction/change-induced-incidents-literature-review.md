# Change-Induced Incidents and Change-Intercept Gates: A Literature Review

This review assembles primary sources on the claim that most production incidents
are caused by changes, and on the mechanisms industry uses to gate those changes.
Quotes are pulled directly from company engineering blogs, regulator filings,
standards bodies, and the SRE / DevOps research canon.

## 1. Google SRE on change as the dominant cause of outages

### 1.1 The "70% of outages" claim (SRE Book, Ch. 1)

The primary source for the frequently cited "~70% of outages are caused by change"
statistic is Chapter 1 of Google's *Site Reliability Engineering* book.

Beyer, Jones, Petoff, Murphy (eds.), *Site Reliability Engineering*, Ch. 1
"Introduction", section "Change Management":
<https://sre.google/sre-book/introduction/>

> "SRE has found that roughly 70% of outages are due to changes in a live
> system. Best practices in this domain use automation to accomplish the
> following:
> - Implementing progressive rollouts
> - Quickly and accurately detecting problems
> - Rolling back changes safely when problems arise
>
> This trio of practices effectively minimizes the aggregate number of users and
> operations exposed to bad changes."

The same chapter frames the dev/ops tension explicitly in change terms:

> "Because most outages are caused by some kind of change — a new configuration,
> a new feature launch, or a new type of user traffic — the two teams' goals
> are fundamentally in tension."

And it describes the classical "gate" response:

> "The ops team attempts to safeguard the running system against the risk of
> change by introducing launch and change gates. For example, launch reviews
> may contain an explicit check for every problem that has ever caused an
> outage in the past — that could be an arbitrarily long list, with not all
> elements providing equal value."

### 1.2 The quantitative breakdown (SRE Workbook, Appendix C)

The 70% number is corroborated by a quantitative breakdown in the SRE Workbook,
based on "thousands of postmortems over the last seven years" (2010–2017):

Murphy, Beyer, Jones, Petoff (eds.), *The Site Reliability Workbook*,
Appendix C "Results of Postmortem Analysis":
<https://sre.google/workbook/postmortem-analysis/>

Top eight outage *triggers* (change-adjacent triggers bolded):

| Trigger | Share |
|---|---|
| **Binary push** | **37%** |
| **Configuration push** | **31%** |
| User behavior change | 9% |
| Processing pipeline | 6% |
| Service provider change | 5% |
| Performance decay | 5% |
| Capacity management | 5% |
| Hardware | 2% |

Binary + configuration pushes alone account for **68%** of triggers at Google,
which is the empirical source of the "~70%" shorthand. Note that "service
provider change" (5%) and "processing pipeline" (6%) are also, in most cases,
change-triggered — so the true upper bound of change-triggered incidents in
Google's dataset is closer to 80%.

Top five root-cause *categories* (same source):

| Category | Share |
|---|---|
| Software | 41.35% |
| Development process failure | 20.23% |
| Complex system behaviors | 16.90% |
| Deployment planning | 6.74% |
| Network failure | 2.75% |

### 1.3 Release Engineering philosophy (SRE Book, Ch. 8)

Dickson, "Release Engineering", *SRE Book* Ch. 8:
<https://sre.google/sre-book/release-engineering/>

The chapter states four principles that have since become the implicit industry
default:

> "Release engineering is guided by an engineering and service philosophy that's
> expressed through four major principles":
>
> 1. **Self-Service Model** — "individual teams can decide how often and when
>    to release new versions of their products. Release processes can be
>    automated to the point that they require minimal involvement by the
>    engineers."
> 2. **High Velocity** — "We have embraced the philosophy that frequent
>    releases result in fewer changes between versions. This approach makes
>    testing and troubleshooting easier."
> 3. **Hermetic Builds** — "If two people attempt to build the same product
>    at the same revision number in the source code repository on different
>    machines, we expect identical results."
> 4. **Enforcement of Policies and Procedures** — "Several layers of security
>    and access control determine who can perform specific operations when
>    releasing a project."

Gated operations (direct quote):

> "Gated operations include:
> - Approving source code changes — this operation is managed through
>   configuration files scattered throughout the codebase
> - Specifying the actions to be performed during the release process
> - Creating a new release
> - Approving the initial integration proposal (which is a request to perform
>   a build at a specific revision number in the source code repository) and
>   subsequent cherry picks
> - Deploying a new release
> - Making changes to a project's build configuration"

On staged rollouts:

> "Our goal is to fit the deployment process to the risk profile of a given
> service. In development or pre-production environments, we may build hourly
> and push releases automatically when all tests pass. For large user-facing
> services, we may push by starting in one cluster and expand exponentially
> until all clusters are updated. For sensitive pieces of infrastructure, we
> may extend the rollout over several days, interleaving them across instances
> in different geographic regions."

On configuration as a first-class change surface:

> "Although configuration management may initially seem a deceptively simple
> problem, configuration changes are a potential source of instability. As a
> result, our approach to releasing and managing system and service
> configurations has evolved substantially over time."

### 1.4 Canarying (SRE Workbook, Ch. 16)

Davidovič & Beyer, "Canarying Releases", *SRE Workbook* Ch. 16:
<https://sre.google/workbook/canarying-releases/>

The Workbook re-asserts the change-incident causality in stronger terms than
the original SRE Book:

> "In Google's experience, a majority of incidents are triggered by binary or
> configuration pushes (see [Results of Postmortem Analysis]). Many kinds of
> software changes can result in a system failure — for example, changes in
> the behavior of an underlying component, changes in the behavior of a
> dependency (such as an API), or a change in configuration like DNS."

Canary definition:

> "We define canarying as a partial and time-limited deployment of a change in
> a service and its evaluation. This evaluation helps us decide whether or not
> to proceed with the rollout. The part of the service that receives the change
> is 'the canary,' and the remainder of the service is 'the control.' …
> Canarying is effectively an A/B testing process."

Requirements:

> "Canarying for a given service requires specific capabilities:
> - A method to deploy the canary change to a subset of the population of the
>   service.
> - An evaluation process to evaluate if the canaried change is 'good' or
>   'bad.'
> - Integration of the canary evaluations into the release process."

### 1.5 Configuration is a first-class change surface (SRE Workbook, Ch. 14)

Davidovič, "Configuration Design and Best Practices", *SRE Workbook* Ch. 14:
<https://sre.google/workbook/configuration-design/>

> "However, configuration tends to differ meaningfully from code in several
> aspects. Changing a system's capabilities via code is typically a lengthy
> and involved process, involving small incremental changes, code reviews,
> and testing. In contrast, changing a single configuration option can have
> dramatic changes on functionality — for example, one bad firewall
> configuration rule may lock you out of your own system. Unlike code,
> configuration often lives in an untested (or even untestable) environment."

This is the canonical statement for why configuration-change gates matter as
much as code-change gates.


## 2. DORA: Change Failure Rate as a first-class stability metric

### 2.1 The four (now five) key metrics

DORA (DevOps Research and Assessment, now a Google Cloud research program)
has measured software delivery performance across thousands of organizations
since 2014. Change failure rate is one of DORA's two primary *stability*
metrics.

*Accelerate State of DevOps 2024 Report*, Google Cloud / DORA:
<https://services.google.com/fh/files/misc/2024_final_dora_report.pdf>

Definitions (direct quotes):

> "**Change fail rate**: the percentage of deployments that cause failures in
> production, requiring hotfixes or rollbacks."

> "**Deployment frequency**: how often application changes are deployed to
> production."

> "**Change lead time**: [the time it takes for a code commit or change to be
> successfully deployed to production]"

> "**Failed deployment recovery time**: the time it takes to recover from a
> failed deployment."

The 2024 report formally split metrics into throughput and stability factors:

> "Change failure rate and rework rate are used when we describe software
> delivery **stability**. This factor measures the likelihood deployments
> unintentionally lead to immediate, additional work."

### 2.2 Elite vs. low-performer change-failure gap

From the same 2024 report, the gap between elite and low performers:

> "When compared to low performers, elite performers realize:
> - 127x faster change lead time
> - 182x more deployments per year
> - 8x lower change failure rate
> - 2,293x faster failed deployment recovery times"

DORA 2024 performance tiers (synthesized from the report; most-widely-cited
summary at <https://www.libertify.com/interactive-library/state-of-devops-2024-dora/>):

| Tier | Lead time | Deploy freq | Change fail rate | Recovery time |
|---|---|---|---|---|
| Elite (19%) | < 1 day | On-demand | ~5% | < 1 hour |
| High (22%) | 1 day – 1 week | Daily – weekly | ~20% | < 1 day |
| Medium (35%) | 1 week – 1 month | Weekly – monthly | ~10% | < 1 day |
| Low (25%) | 1–6 months | Monthly – semi-annual | ~40% | 1 week – 1 month |

Note the inversion: "high" performers have *higher* change-fail than "medium"
performers in 2024 data. DORA interprets this as "throughput without sufficient
stability investment":

> "Often, teams struggle to maintain a low change fail rate when they're
> pushing code fast (can be from lack of QA automation, code review, test
> coverage, etc.)."

### 2.3 Change-fail rate as a proxy for rework

The 2024 report is notable for introducing a fifth metric, rework rate:

> "We have a longstanding hypothesis that the change failure rate metric works
> as a proxy for the amount of rework a team is asked to do. When a delivery
> fails, this requires the team to fix the change, likely by introducing
> another change. … Our data analysis confirmed our hypothesis that rework
> rate and change failure rate are related. Together, these two metrics
> create a reliable factor of software delivery stability."

This matters for any change-gate argument: every prevented bad change is not
just a prevented incident but a prevented rework cycle.

## 3. VOID: What the data actually says (and doesn't)

VOID (Verica Open Incident Database) is the largest public corpus of
software-incident reports: nearly 10,000 incidents from ~600 companies by the
time of the 2022 report. It is the closest thing the industry has to an
independent counterpoint to Google's internal statistics.

Landing page: <https://www.thevoid.community/>

### 3.1 What VOID actually claims

Unlike Google SRE, VOID does **not** publish a single "% of incidents are
change-induced" number. Its 2022 and 2023 reports focus instead on
*metric critiques*. The key findings, directly from Verica's own press
release and summaries:

Verica press release (2022 VOID Report, Dec 13, 2022):
<https://www.businesswire.com/news/home/20221213005501/en/Verica-Announces-the-Second-Annual-Verica-Open-Incident-Database-VOID-Report-to-Make-the-Internet-More-Resilient>

Direct quotes:

> "No company is immune from incidents. Incidents happen in organizations of
> all sizes, from startups to the Fortune 10."

> "SREs and others in similar roles should retire MTTR as a key metric. This
> year's report confirms that MTTR isn't a viable metric for the reliability
> of complex software systems for a myriad of reasons, particularly because
> averages of duration data lie."

> "Common assumptions around incident duration and severity are debunked.
> Incident duration and severity are not related."

> "Organizations are moving away from shortsighted approaches like RCA. Root
> Cause Analysis appears to be on the decline."

Courtney Nash (VOID lead researcher), in the same release:

> "We were surprised to find no relationship between the length of an incident
> and how 'bad' it was. … Companies can have long or short incidents that
> are very minor or quite serious, and every combination in between. Not only
> can duration not tell a team how reliable or effective they are, it also
> doesn't convey anything useful about the impact of the event or the effort
> required to deal with it."

### 3.2 VOID on change (2024 automation report)

The 2024 VOID report narrowed its focus to automation's role in incidents.
From the Russian-language summary of the 2024 report (primary source is the
downloadable VOID 2024 PDF, summarized at
<https://enabling.team/insights/void-report-2024>):

From a hand-coded sample of 189 incidents, VOID coded automation as a
contributing factor in these ways:
- Manual intervention required for remediation: **75%**
- Automation involved in detection: **34%**
- Automation used in recovery: **20%**
- Automation appeared in post-incident action items: **37%**
- Automation hindered remediation: **14%**

The central thesis of the 2024 report (Nash): automation that was built to
reduce risk *introduces new complexity and new failure modes*. This is a
direct challenge to the naive "more automation = fewer incidents"
reading of Google SRE.

### 3.3 VOID vs. Google SRE: what's actually said

| Claim | Google SRE (Book + Workbook) | VOID (2021–2024 reports) |
|---|---|---|
| "% of outages caused by change" | ~70% (Book Ch. 1); 68% binary+config (Workbook App. C) | Does **not** publish this number |
| MTTR as a reliability metric | Used freely; MTTR × MTTF framework | **Explicitly rejects** as misleading for complex systems |
| Root-cause analysis | Standard postmortem format has root cause + trigger | Argues RCA framing is in decline and misleading |
| Source of evidence | Internal Google postmortems | Public incident writeups across ~600 companies |
| Primary prescription | Canary, progressive rollout, error budgets | Qualitative incident analysis; "resilience saves time" |

The important methodological point: **the 70% number is Google's internal
finding and has not been independently replicated at public-corpus scale.**
VOID, which could in principle replicate it, has chosen not to because they
view "cause" framing itself as suspect. So a careful writeup should treat 70%
as "Google's internal postmortem data over 2010–2017, confirmed in Workbook
Appendix C" rather than as a universal constant.


## 4. Industry adjacent sources: Netflix, Meta, Amazon

### 4.1 Netflix: Kayenta / Automated Canary Analysis

Netflix and Google co-developed Kayenta, the open-source Automated Canary
Analysis (ACA) platform, as an extension of Netflix's internal system.

Netflix TechBlog, "Automated Canary Analysis at Netflix with Kayenta" (2018):
<https://netflixtechblog.com/automated-canary-analysis-at-netflix-with-kayenta-3260bc7acc69>

> "Kayenta leverages lessons learned over the years of delivering rapid and
> reliable changes into production at Netflix. It is a crucial component of
> delivery at Netflix as it reduces the risk from making changes in our
> production environment."

> "The Kayenta platform is responsible for assessing the risk of a canary
> release and checks for significant degradation between the baseline and
> canary. This is comprised of two primary stages: metric retrieval and
> judgment."

> "The primary metric comparison algorithm in Kayenta uses confidence
> intervals, computed by the Mann-Whitney U test, to classify whether a
> significant difference exists between the canary and baseline metrics."

Joint Google/Netflix launch post:
<https://cloud.google.com/blog/products/gcp/introducing-kayenta-an-open-automated-canary-analysis-tool-from-google-and-netflix>

> "To perform continuous delivery at any scale, you need to be able to release
> software changes not just at high velocity, but safely as well. … Kayenta
> fetches user-configured metrics from their sources, runs statistical tests,
> and provides an aggregate score for the canary. Based on the score and set
> limits for success, Kayenta can automatically promote or fail the canary,
> or trigger a human approval path."

This is the academic-quality primary source for "statistical canary analysis
as a change gate."

### 4.2 Meta: October 4, 2021 BGP outage (config change)

Meta, "More details about the October 4 outage", Oct 5, 2021:
<https://engineering.fb.com/2021/10/05/networking-traffic/outage-details/>

Direct attribution to a change:

> "During one of these routine maintenance jobs, a command was issued with
> the intention to assess the availability of global backbone capacity, which
> unintentionally took down all the connections in our backbone network,
> effectively disconnecting Facebook data centers globally. Our systems are
> designed to audit commands like these to prevent mistakes like this, but
> a bug in that audit tool prevented it from properly stopping the command."

Meta, initial outage note, Oct 4, 2021:
<https://engineering.fb.com/2021/10/04/networking-traffic/outage/>

> "Our engineering teams have learned that configuration changes on the
> backbone routers that coordinate network traffic between our data centers
> caused issues that interrupted this communication. … We want to make
> clear that there was no malicious activity behind this outage — its root
> cause was a faulty configuration change on our end."

This is the paradigmatic example of an infrastructure-config change taking
down a $500B company for ~6 hours. The change-intercept (Meta's audit tool)
existed but was itself buggy.

### 4.3 Amazon: Operator-initiated change incidents

**S3 US-EAST-1 (February 28, 2017)** — operator command with a typo:
<https://aws.amazon.com/message/41926/>

> "At 9:37AM PST, an authorized S3 team member using an established playbook
> executed a command which was intended to remove a small number of servers
> for one of the S3 subsystems that is used by the S3 billing process.
> Unfortunately, one of the inputs to the command was entered incorrectly
> and a larger set of servers was removed than intended."

AWS's remediation is textbook change-gating:

> "We have modified this tool to remove capacity more slowly and added
> safeguards to prevent capacity from being removed when it will take any
> subsystem below its minimum required capacity level. This will prevent an
> incorrect input from triggering a similar event in the future. We are also
> auditing our other operational tools to ensure we have similar safety
> checks."

**US-EAST-1 (December 7, 2021)** — automated scaling change with a latent bug:
<https://aws.amazon.com/message/12721/>

> "At 7:30 AM PST, an automated activity to scale capacity of one of the AWS
> services hosted in the main AWS network triggered an unexpected behavior
> from a large number of clients inside the internal network. This resulted
> in a large surge of connection activity that overwhelmed the networking
> devices between the internal network and the main AWS network."

> "Our networking clients have well tested request back-off behaviors that are
> designed to allow our systems to recover from these sorts of congestion
> events, but, a latent issue prevented these clients from adequately backing
> off during this event. This code path has been in production for many years
> but the automated scaling activity triggered a previously unobserved
> behavior."

Two distinct change surfaces in one incident: (1) an automated capacity change
and (2) a latent code path exercised for the first time by that change.


## 5. Taxonomy of change categories, each with a primary-source incident

This is the taxonomy the user asked for, with at least one primary-source
post-mortem per category.

### 5.1 Source code changes (application logic)

**Knight Capital, August 1, 2012** — $460M loss in 45 minutes from a bad
deployment that left unused legacy code paths wired to a repurposed flag.

SEC Order instituting proceedings (primary regulatory filing):
<https://www.sec.gov/files/litigation/admin/2013/34-70694.pdf>

> "Beginning on July 27, 2012, Knight deployed the new RLP code in SMARS in
> stages by placing it on a limited number of servers in SMARS on successive
> days. During the deployment of the new code, however, one of Knight's
> technicians did not copy the new code to one of the eight SMARS computer
> servers. Knight did not have a second technician review this deployment
> and no one at Knight realized that the Power Peg code had not been removed
> from the eighth server, nor the new RLP code added. Knight had no written
> procedures that required such a review."

> "Knight also violated the requirements of Rule 15c3-5(b) because Knight did
> not have technology governance controls and supervisory procedures
> sufficient to ensure the orderly deployment of new code or to prevent the
> activation of code no longer intended for use in Knight's current operations
> but left on its servers that were accessing the market; and Knight did not
> have controls and supervisory procedures reasonably designed to guide
> employees' responses to significant technological and compliance issues."

The canonical regulator-documented case of "a code deployment that wasn't
uniform across servers + no deployment gate = firm-ending outage."

### 5.2 Binary / artifact promotions

**CrowdStrike Falcon sensor, July 19, 2024** — Channel File 291 promoted to
8.5M Windows hosts, triggering BSOD worldwide.

CrowdStrike Preliminary Post Incident Review:
<https://www.crowdstrike.com/falcon-content-update-remediation-and-guidance-hub/>
(Full PIR PDF:
<https://www.crowdstrike.com/content/dam/crowdstrike/www/en-us/wp/2024/07/Channel-File-291-Incident-Root-Cause-Analysis-Executive-Summary.pdf>)

> "On July 19, 2024, as part of regular operations, CrowdStrike released a
> content configuration update for the Windows sensor that resulted in a
> system crash (Blue Screen of Death, or BSOD) on impacted systems."

> "The new IPC Template Type defined 21 input parameter fields, but the
> integration code that invoked the Content Interpreter with Channel File 291's
> Template Instances supplied only 20 input values to match against. This
> parameter count mismatch evaded multiple layers of build validation and
> testing, as it was not discovered during the sensor release testing process,
> the Template Type (using a test Template Instance) stress testing, or the
> first several successful deployments of IPC Template Instances in the field."

Remediations (direct quote) include the kinds of gates everyone else in this
review uses:

> "Additional deployment layers and acceptance checks for Rapid Response
> Content … Staggered deployment strategy for Rapid Response Content in
> which updates are gradually deployed to larger portions of the sensor
> base, starting with a canary deployment. Improve monitoring for both
> sensor and system performance, collecting feedback during Rapid Response
> Content deployment to guide a phased rollout."

This is the 2024 industry-defining case of "artifact promotion without
staged rollout or canary."

### 5.3 Runtime configuration changes (feature flags, WAF rules, env)

**Cloudflare WAF, July 2, 2019** — a regex change caused global CPU
exhaustion.

Graham-Cumming, "Details of the Cloudflare outage on July 2, 2019":
<https://blog.cloudflare.com/details-of-the-cloudflare-outage-on-july-2-2019/>

> "On July 2, we deployed a new rule in our WAF Managed Rules that caused
> CPUs to become exhausted on every CPU core that handles HTTP/HTTPS traffic
> on the Cloudflare network worldwide. We are constantly improving WAF
> Managed Rules to respond to new vulnerabilities and threats. In May, for
> example, we used the speed with which we can update the WAF to push a rule
> to protect against a serious SharePoint vulnerability. … Unfortunately,
> last Tuesday's update contained a regular expression that backtracked
> enormously and exhausted CPU used for HTTP/HTTPS serving traffic on our
> network worldwide."

Post-mortem on the change process itself:

> "We are making the following changes:
>  - Re-introduce the excessive CPU usage protection that got removed.
>  - Manually reviewing all 3,868 rules in the WAF Managed Rules to find and
>    correct any other instances of possible excessive backtracking.
>  - Introduce performance profiling for all rules to the test suite.
>  - Change the WAF Managed Rules release process to perform staged rollouts
>    in the same manner used for other software at Cloudflare while retaining
>    the ability to do emergency global deployment for active attacks."

The explicit admission: the WAF config pipeline lacked the staged-rollout gate
their code pipeline already had.

### 5.4 Infrastructure changes (routers, BGP, cloud console)

**Cloudflare, June 21, 2022** — network configuration change (MCP rollout).

Camara & Tomlinson, "Cloudflare outage on June 21, 2022":
<https://blog.cloudflare.com/cloudflare-outage-on-june-21-2022/>

> "Today, 21 June 2022, Cloudflare suffered an outage that affected traffic in
> 19 of our data centers. … This outage was caused by a change that was
> part of a long-running project to increase resilience in our busiest
> locations. A change to the network configuration in those locations
> caused an outage which started at 06:27 UTC. At 06:58 UTC the first data
> center was brought back online and by 07:42 UTC all data centers were
> online and working correctly."

> "While deploying a change to our prefix advertisement policies, a
> re-ordering of terms caused us to withdraw a critical subset of prefixes.
> … The change was part of a long-running project to convert all our BGP
> sessions into a more structured format."

See §4.2 (Meta Oct 4, 2021) for the complementary example at hyperscaler
scale.

### 5.5 Data changes (schema migrations, backfills, accidental deletion)

**GitLab.com, January 31, 2017** — production DB directory wiped.

GitLab postmortem, "Postmortem of database outage of January 31":
<https://about.gitlab.com/blog/postmortem-of-database-outage-of-january-31/>

> "On January 31st 2017, we experienced a major service outage for one of
> our products, the online service GitLab.com. The outage was caused by an
> accidental removal of data from our primary database server."

> "This resulted in YP thinking that db2.cluster is not running. At 2017/01/31
> 23:27 YP — thinking that db2.cluster refused to connect due to an earlier
> rsync process not cleaning up properly — decides to remove the directory.
> A second or two after running `rm -rvf /var/opt/gitlab/postgresql/data`
> on db1.cluster.gitlab.com, he notices the terminal he's running the
> command on is connected to db1.cluster.gitlab.com, not db2.cluster as
> intended."

Backup-gate failure mode also documented:

> "Out of 5 backup/replication techniques deployed none are working reliably
> or set up in the first place. We ended up restoring a 6 hour old backup.
> … pg_dump may be failing because PostgreSQL 9.2 binaries are being run
> instead of 9.6 binaries."

### 5.6 Dependency / OS upgrade changes

**Datadog, March 8, 2023** — automatic systemd security update.

Datadog postmortem, "2023-03-08 Multi-region Infrastructure Connectivity Issue":
<https://www.datadoghq.com/blog/2023-03-08-multiregion-infrastructure-connectivity-issue/>

> "The trigger of the outage has now been clearly identified, and the outage
> itself is not the result of an attack or other malicious act of any kind.
> On the contrary, it was the result of a security update automatically
> applied to several Virtual Machines (VMs) in our fleet."

> "The root cause of the outage was the combination of:
>  - A latent bug in the version of systemd shipped by Ubuntu 22.04 that was
>    triggered when systemd-networkd was forcefully restarted by the
>    security update applied.
>  - A legacy automation process on VMs from older Datadog clusters which
>    applied the security update during a scheduled maintenance window."

The dependency change (OS package) had a time-fuse behavior: it was applied
but dormant until systemd-networkd restarted.

### 5.7 Traffic / load-shift changes

**AWS US-EAST-1, December 7, 2021** — an *internal* traffic change (auto-scaling
activity on an internal service) caused external impact. See §4.3 for the
full quote.

For a user-initiated traffic change, see the Meta backbone incident (§4.2)
in which a command intended to *measure* backbone capacity *shifted* it to
zero.

### 5.8 Security / policy changes (certs, IAM, firewall)

**Microsoft Azure AD / Microsoft 365, January 25, 2023** — a planned change
to the Wide Area Network removed all routes.

Microsoft post-incident review summary (via The Register primary reproduction
of the Microsoft PIR text):
<https://www.theregister.com/2023/01/26/microsoft_365_outage_root_cause/>
(Microsoft's own posting: <https://azure.status.microsoft/en-us/status/history/>, event tracking ID VSG1-B90.)

Microsoft's stated root cause:

> "We determined that a change made to the Microsoft Wide Area Network (WAN)
> impacted connectivity between clients on the internet to Azure, connectivity
> between services within regions, as well as ExpressRoute connections. …
> As part of a planned change to update the IP address on a WAN router,
> a command given to the router caused it to send messages to all other
> routers in the WAN, which resulted in all of them recomputing their
> adjacency and forwarding tables."

For a certificate-change example, see Let's Encrypt's March 2020 CAA
re-validation incident:
<https://community.letsencrypt.org/t/revoking-certain-certificates-on-march-4/114864>

> "On 2020-02-29 UTC, Let's Encrypt found a bug in our CAA code. Our CA
> software, Boulder, checks for CAA records at the same time it validates a
> subscriber's control of a domain name. Most subscribers issue a certificate
> immediately after domain control validation, but we consider a validation
> good for 30 days. … When a certificate request contained N domain names
> that needed CAA rechecking, Boulder would pick one domain name and check
> it N times."

The fix required revoking 3M certificates — a policy/security change whose
correctness was gated only by code review, not by a runtime policy test.

### 5.9 Observability / monitoring changes

**Honeycomb, June 3, 2024** — Kafka consumer-group rebalance after a deploy
broke ingest.

Honeycomb postmortem, "Incident Review: Shepherd Partitioning Problems":
<https://www.honeycomb.io/blog/incident-review-shepherd-partitioning-problems>

> "The deploy seemed like it went well, but about 20 minutes later, a Kafka
> consumer group rebalance caused a subset of our ingest pipeline to stop
> consuming from Kafka. Our monitoring didn't detect the issue for about 10
> minutes because our alerting was only looking at aggregate consumer-lag
> across the entire fleet."

For a more famous monitoring-as-change example see the Target Monitoring
Incident described in Nash & Lorin's *Learning from Incidents* series,
where the promotion of a new dashboard query masked a production issue.

### Taxonomy recap

| Category | Canonical primary source |
|---|---|
| Source code | Knight Capital SEC filing 2013 |
| Binary/artifact | CrowdStrike Channel 291 PIR 2024 |
| Runtime config (WAF/flags) | Cloudflare July 2019 postmortem |
| Infrastructure (BGP/routers) | Cloudflare June 2022; Meta Oct 2021 |
| Data / migrations | GitLab Jan 2017 postmortem |
| Dependency / OS | Datadog March 2023 postmortem |
| Traffic / load-shift | AWS US-EAST-1 Dec 2021 |
| Security / policy | Microsoft Jan 2023; Let's Encrypt March 2020 |
| Observability / monitoring | Honeycomb June 2024 |


## 6. Tool taxonomy: how each change surface gets gated today

Organized by the change surface the tool sits in front of.

### 6.1 Application source code

**CodeRabbit** — AI-driven PR review.

CodeRabbit docs, "Code review overview":
<https://docs.coderabbit.ai/guides/code-review-overview/>

> "CodeRabbit automatically analyzes every pull request with a multi-layered
> approach that combines the best of AI and industry-standard tools. …
> Spot potential runtime errors, null pointer exceptions, race conditions,
> and logic flaws before deployment."

> "CodeRabbit integrates with over 20 popular static analysis, security, and
> linting tools … Security scanning, SAST (Static Application Security
> Testing) to detect vulnerabilities … Secret detection to identify
> hardcoded credentials, API keys, and sensitive information."

Sits on the git-provider PR webhook; its "gate" is an advisory review plus,
optionally, a required-check status on merge.

**GitHub branch protection / merge queue**, **GitLab MR approval rules**,
**Gerrit Code Review** — platform-native change gates for source control.

### 6.2 IaC / Terraform plans

**Atlantis** — Terraform PR automation.

Atlantis project docs:
<https://www.runatlantis.io/>

> "Atlantis is an application for automating Terraform via pull requests. …
> Runs `terraform plan`, `import`, `apply` remotely and comments back on
> the pull request with the output. … You can require approval
> (`atlantis apply` blocked until an approval is received) before the pull
> request is applied so nothing happens accidentally."

**Spacelift** — managed IaC runner with Rego-based policies.

Spacelift docs, "Plan policy":
<https://docs.spacelift.io/concepts/policy/terraform-plan-policy>

> "Plan policies are the only ones with access to the actual changes to the
> managed resources, making them the best place to enforce organizational
> rules and best practices as well as do automated code review. There are
> two types of rules here that Spacelift will care about: `deny` and
> `warn`."

> "Sample use cases:
>  - require certain types of changes (e.g. security group changes) to
>    receive multiple human approvals;
>  - prevent using specific Terraform providers;
>  - ensure specific resource types are never destroyed;
>  - enforce tagging conventions;
>  - enforce naming conventions."

**env0**, **Scalr**, **Terraform Cloud (run tasks)** — similar managed IaC
runners with policy stages.

**HashiCorp Sentinel** — the vendor-native policy-as-code layer embedded in
Terraform Cloud/Enterprise, Vault, Consul, and Nomad.

HashiCorp Sentinel docs, "Policy as Code":
<https://developer.hashicorp.com/sentinel/docs/concepts/policy-as-code>

> "Sentinel is HashiCorp's policy as code framework embedded in HashiCorp
> Enterprise products. It enables fine-grained, logic-based policy
> decisions, and can be extended to use information from external sources."

> "All of HashiCorp's enterprise products that enable Sentinel also have an
> `enforcement level` for every policy. This allows varying levels of
> strictness for the same policy depending on the use case. Three
> enforcement levels are supported: advisory, soft-mandatory, hard-mandatory."

### 6.3 Static IaC scanning (pre-plan)

**Checkov** (Bridgecrew/Prisma Cloud):
<https://www.checkov.io/>

> "Checkov is a static code analysis tool for scanning infrastructure as code
> (IaC) files for misconfigurations that may lead to security or compliance
> problems. Checkov includes more than 750 predefined policies to check for
> common misconfiguration issues. Checkov also supports the creation and
> contribution of custom policies."

**Trivy (tfsec)** — as of 2022, Aqua Security merged tfsec into Trivy; tfsec
is now in maintenance mode.

tfsec repository notice:
<https://github.com/aquasecurity/tfsec>

> "tfsec's Terraform scanning engine is now part of Trivy. Trivy is the open
> source, all-in-one security scanner from Aqua Security, and includes the
> same Terraform scanning capabilities that tfsec provides."

**Conftest** — OPA CLI wrapper for testing structured-config files (Terraform
plans, Kubernetes manifests, Dockerfiles).

Conftest docs:
<https://www.conftest.dev/>

> "Conftest is a utility to help you write tests against structured
> configuration data. For instance, you could write tests for your
> Kubernetes configurations, Tekton pipeline definitions, Terraform code,
> Serverless configs or any other structured data. Conftest relies on the
> Rego language from Open Policy Agent for writing the assertions."

### 6.4 Kubernetes admission control

**OPA Gatekeeper** — validating/mutating admission webhook backed by OPA.

Gatekeeper docs:
<https://open-policy-agent.github.io/gatekeeper/website/docs/>

> "Gatekeeper is a validating (mutating TBA) webhook that enforces CRD-based
> policies executed by Open Policy Agent, a policy engine for Cloud Native
> environments hosted by CNCF as a graduated project."

> "In addition to the admission scenario, Gatekeeper's audit functionality
> allows administrators to see what resources are currently violating any
> given policy."

**Kyverno** — the Kubernetes-native alternative to Gatekeeper; policies are
YAML rather than Rego.

Kyverno docs:
<https://kyverno.io/docs/introduction/>

> "Kyverno is a policy engine designed for Kubernetes. With Kyverno,
> policies are managed as Kubernetes resources and no new language is
> required to write policies. … Kyverno policies can validate, mutate,
> generate, and cleanup Kubernetes resources, and verify image signatures
> and artifacts to help secure the software supply chain."

**OPA (standalone)** — general-purpose engine.

OPA docs:
<https://www.openpolicyagent.org/docs/>

> "The Open Policy Agent (OPA, pronounced 'oh-pa') is an open source,
> general-purpose policy engine that unifies policy enforcement across the
> stack. OPA provides a high-level declarative language that lets you
> specify policy as code and simple APIs to offload policy decision-making
> from your software."

### 6.5 Feature-flag / runtime-config changes

**LaunchDarkly Approvals**:

LaunchDarkly docs, "Approvals":
<https://docs.launchdarkly.com/home/releases/approvals/>

> "Approvals allow you to request review for changes to feature flags, AI
> Configs, and segments. When an account member plans a change to a feature
> flag, AI Config, or segment, they have the option to request approval for
> that change from a member or team. … These review-style approvals mimic
> common code review workflows, such as pull request (PR) reviews in
> GitHub."

> "Approvals and required approvals work with audit log webhooks and the
> Jira integration. You can also require approvals for certain types of
> changes, or on flags in specific environments, by configuring approval
> settings at the project or environment level."

**Statsig**, **Unleash**, **ConfigCat**, **Flagsmith**, **Split** — similar
PR-style approval workflows; Statsig's most recent "Change log + approvals"
workflow follows the same pattern.

**AWS AppConfig** — AWS's native runtime config layer with deployment
strategies, equivalent to canarying at the config level:
<https://docs.aws.amazon.com/appconfig/latest/userguide/appconfig-creating-deployment-strategy.html>

> "A deployment strategy defines how AppConfig deploys a configuration to
> your targets. … Predefined deployment strategies include AppConfig.Linear50PercentEvery30Seconds,
> AppConfig.Canary10Percent20Minutes, and AppConfig.AllAtOnce."

### 6.6 Progressive delivery / canary analysis

**Spinnaker + Kayenta** — see §4.1.

**Argo Rollouts**:
<https://argoproj.github.io/argo-rollouts/>

> "Argo Rollouts is a Kubernetes controller and set of CRDs which provide
> advanced deployment capabilities such as blue-green, canary, canary
> analysis, experimentation, and progressive delivery features to
> Kubernetes."

**Flagger** (Flux project):
<https://fluxcd.io/flagger/>

> "Flagger is a progressive delivery tool that automates the release process
> for applications running on Kubernetes. It reduces the risk of introducing
> a new software version in production by gradually shifting traffic to the
> new version while measuring metrics and running conformance tests."

### 6.7 Change-surface matrix

| Change surface | Gate tools (open-source / commercial) |
|---|---|
| App source code | GitHub branch protection, GitLab MR rules, Gerrit, CodeRabbit |
| CI/CD pipeline | GitHub Actions required checks, GitHub Environments w/ reviewers, Jenkins RBAC |
| Binary / artifact promotion | Artifactory release bundles, Cosign + Kyverno/Sigstore policy, Spinnaker manual judgment |
| IaC plan (Terraform) | Atlantis, Spacelift, Terraform Cloud + Sentinel/OPA, env0, Scalr |
| IaC static scan | Checkov, Trivy (tfsec), Conftest (OPA), KICS |
| K8s admission | OPA Gatekeeper, Kyverno, Datree, jsPolicy |
| Feature flags / runtime config | LaunchDarkly Approvals, Statsig, Unleash, ConfigCat, Split, AWS AppConfig |
| Progressive delivery | Argo Rollouts, Flagger, Spinnaker + Kayenta, AWS CodeDeploy |
| Cert / secret rotation | HashiCorp Vault, cert-manager, AWS Secrets Manager rotation |
| Cloud console / IAM | AWS Organizations SCPs, GCP Org Policies, Azure Policy, Cloud Custodian |
| DB schema / migration | gh-ost, pt-online-schema-change, Liquibase with approval, Bytebase |

## 7. Synthesis

1. **The "70%" number is real but local.** Google's internal data (SRE Book
   Ch. 1, Workbook Appendix C) supports 68–70% of outages being change-
   triggered. No public corpus (including VOID) has independently
   replicated that exact number, and VOID explicitly declines to frame
   incidents that way.

2. **DORA confirms change stability is a first-order metric.** Change
   Failure Rate is one of five DORA metrics and the 2024 report ties it
   directly to "rework rate." Elite performers ship ~182× more deploys
   while keeping change-fail ~8× lower than low performers.

3. **Change surfaces are plural.** Real incidents come from at least nine
   distinct change surfaces (§5). Any change-intercept strategy that only
   gates "git push to main" is covering a small fraction of the risk.

4. **Existing tools are surface-specific.** There is no single product that
   gates code + Terraform + K8s admission + feature flags + schema +
   IAM. Each surface has its own dominant tool (§6). Organizations assemble
   a lattice.

5. **Policy-as-code (OPA/Sentinel/Kyverno) is the lingua franca.** Across
   Spacelift, Gatekeeper, Conftest, and Sentinel, Rego-or-Rego-like policy
   languages have become the de facto way to express change gates in a
   tool-agnostic way.

6. **Canarying / progressive delivery is the prescribed runtime gate.**
   Every primary source that prescribes a gate (SRE Book Ch. 8, SRE
   Workbook Ch. 16, Netflix Kayenta, CrowdStrike's own post-2024 PIR)
   converges on the same prescription: deploy to a small population,
   evaluate automatically, promote or roll back.

7. **Known failure mode: the gate itself.** Meta Oct 2021 took down
   Facebook precisely because an audit/gate tool had a bug
   (§4.2). Gates are themselves change surfaces and need gates.


## 8. Primary-source citation index

### Google SRE
- SRE Book, Ch. 1 "Introduction": <https://sre.google/sre-book/introduction/>
- SRE Book, Ch. 8 "Release Engineering": <https://sre.google/sre-book/release-engineering/>
- SRE Workbook, Ch. 14 "Configuration Design and Best Practices": <https://sre.google/workbook/configuration-design/>
- SRE Workbook, Ch. 16 "Canarying Releases": <https://sre.google/workbook/canarying-releases/>
- SRE Workbook, Appendix C "Results of Postmortem Analysis": <https://sre.google/workbook/postmortem-analysis/>

### DORA
- 2024 Accelerate State of DevOps Report (PDF): <https://services.google.com/fh/files/misc/2024_final_dora_report.pdf>
- 2023 Accelerate State of DevOps Report landing page: <https://dora.dev/research/2023/dora-report/>
- DORA metrics reference: <https://dora.dev/quickcheck/>

### VOID
- VOID community landing: <https://www.thevoid.community/>
- 2022 VOID Report press release (Verica): <https://www.businesswire.com/news/home/20221213005501/en/Verica-Announces-the-Second-Annual-Verica-Open-Incident-Database-VOID-Report-to-Make-the-Internet-More-Resilient>
- VOID reports page: <https://www.thevoid.community/report>

### Incident post-mortems by category (§5)
- Knight Capital SEC filing: <https://www.sec.gov/files/litigation/admin/2013/34-70694.pdf>
- CrowdStrike Channel 291 PIR: <https://www.crowdstrike.com/falcon-content-update-remediation-and-guidance-hub/>
- Cloudflare WAF July 2019: <https://blog.cloudflare.com/details-of-the-cloudflare-outage-on-july-2-2019/>
- Cloudflare June 2022: <https://blog.cloudflare.com/cloudflare-outage-on-june-21-2022/>
- Meta Oct 4 2021 (details): <https://engineering.fb.com/2021/10/05/networking-traffic/outage-details/>
- Meta Oct 4 2021 (initial): <https://engineering.fb.com/2021/10/04/networking-traffic/outage/>
- AWS S3 Feb 2017: <https://aws.amazon.com/message/41926/>
- AWS US-EAST-1 Dec 2021: <https://aws.amazon.com/message/12721/>
- GitLab Jan 2017: <https://about.gitlab.com/blog/postmortem-of-database-outage-of-january-31/>
- Datadog March 2023: <https://www.datadoghq.com/blog/2023-03-08-multiregion-infrastructure-connectivity-issue/>
- Let's Encrypt CAA March 2020: <https://community.letsencrypt.org/t/revoking-certain-certificates-on-march-4/114864>
- Microsoft Jan 2023 WAN: <https://azure.status.microsoft/en-us/status/history/>

### Change-gating tools
- OPA: <https://www.openpolicyagent.org/docs/>
- OPA Gatekeeper: <https://open-policy-agent.github.io/gatekeeper/website/docs/>
- Kyverno: <https://kyverno.io/docs/introduction/>
- Conftest: <https://www.conftest.dev/>
- Checkov: <https://www.checkov.io/>
- Trivy (ex-tfsec): <https://github.com/aquasecurity/tfsec>
- HashiCorp Sentinel: <https://developer.hashicorp.com/sentinel/docs/concepts/policy-as-code>
- Atlantis: <https://www.runatlantis.io/>
- Spacelift plan policy: <https://docs.spacelift.io/concepts/policy/terraform-plan-policy>
- CodeRabbit: <https://docs.coderabbit.ai/guides/code-review-overview/>
- LaunchDarkly Approvals: <https://docs.launchdarkly.com/home/releases/approvals/>
- Argo Rollouts: <https://argoproj.github.io/argo-rollouts/>
- Flagger: <https://fluxcd.io/flagger/>
- Kayenta (Netflix blog): <https://netflixtechblog.com/automated-canary-analysis-at-netflix-with-kayenta-3260bc7acc69>
- Kayenta (Google Cloud): <https://cloud.google.com/blog/products/gcp/introducing-kayenta-an-open-automated-canary-analysis-tool-from-google-and-netflix>
- AWS AppConfig deployment strategies: <https://docs.aws.amazon.com/appconfig/latest/userguide/appconfig-creating-deployment-strategy.html>

