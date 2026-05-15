---
name: thousandeyes
id: thousandeyes
description: "ThousandEyes network intelligence integration for monitoring tests, alerts, agents, Internet Insights outages, dashboards, and BGP routes during RCA investigations"
category: observability
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.thousandeyes_tool
  function: is_thousandeyes_connected
tools:
  - thousandeyes_list_tests
  - thousandeyes_get_test_detail
  - thousandeyes_get_test_results
  - thousandeyes_get_alerts
  - thousandeyes_get_alert_rules
  - thousandeyes_get_agents
  - thousandeyes_get_endpoint_agents
  - thousandeyes_get_internet_insights
  - thousandeyes_get_dashboards
  - thousandeyes_get_dashboard_widget
  - thousandeyes_get_bgp_monitors
index: "Network intelligence -- tests, alerts, agents, Internet Insights outages, dashboards, BGP monitors"
rca_priority: 4
allowed-tools: thousandeyes_list_tests, thousandeyes_get_test_detail, thousandeyes_get_test_results, thousandeyes_get_alerts, thousandeyes_get_alert_rules, thousandeyes_get_agents, thousandeyes_get_endpoint_agents, thousandeyes_get_internet_insights, thousandeyes_get_dashboards, thousandeyes_get_dashboard_widget, thousandeyes_get_bgp_monitors
metadata:
  author: aurora
  version: "1.0"
---

# ThousandEyes Integration

## Overview
ThousandEyes network intelligence integration for investigating network-layer issues during Root Cause Analysis. ThousandEyes monitors network paths, DNS, BGP routing, HTTP availability, page load performance, and detects macro-scale Internet outages affecting connectivity.

## Instructions

### Test Discovery and Results
1. `thousandeyes_list_tests(test_type=TYPE)` -- List all configured tests. Optionally filter by type: `agent-to-server`, `agent-to-agent`, `bgp`, `dns-server`, `dns-trace`, `dnssec`, `http-server`, `page-load`, `web-transactions`, `api`, `sip-server`, `voice`. Call this first to discover available tests.
2. `thousandeyes_get_test_detail(test_id='ID')` -- Full configuration for a single test including server, interval, protocol, alert rules, and assigned agents.
3. `thousandeyes_get_test_results(test_id='ID', result_type=TYPE, window='1h')` -- Get results for a specific test. Result types:
   - `'network'` -- latency, loss, jitter
   - `'http'` -- response time, availability
   - `'path-vis'` -- hop-by-hop trace
   - `'dns'` -- DNS resolution
   - `'bgp'` -- BGP routes
   - `'page-load'` -- full waterfall
   - `'web-transactions'` -- scripted browser
   - `'ftp'` -- FTP results
   - `'api'` -- API test
   - `'sip'` -- SIP/VoIP
   - `'voice'` -- MOS, jitter
   - `'dns-trace'` -- trace chain
   - `'dnssec'` -- DNSSEC validation

### Alerts and Alert Rules
4. `thousandeyes_get_alerts(state='active', severity='major', window='1h')` -- Get active or recent alerts. Filter by state (`active`/`cleared`) and severity (`major`/`minor`/`info`). Shows alert rules, affected agents, and violation counts.
5. `thousandeyes_get_alert_rules()` -- List all alert rule definitions. Shows rule expressions, thresholds, severity, and which tests each rule applies to.

### Monitoring Agents
6. `thousandeyes_get_agents(agent_type='enterprise')` -- List cloud and enterprise monitoring agents. Filter by type (`cloud`/`enterprise`). Shows agent location, state, and IP addresses.
7. `thousandeyes_get_endpoint_agents()` -- List endpoint agents installed on employee devices. Shows device name, OS, platform, location, public IP, and VPN status.

### Internet Insights
8. `thousandeyes_get_internet_insights(outage_type='network', window='6h')` -- Internet Insights outage data. Set `outage_type` to `'network'` for ISP/transit outages or `'application'` for SaaS/CDN outages. Detects macro-scale internet issues affecting users.

### Dashboards
9. `thousandeyes_get_dashboards(dashboard_id='ID')` -- List all dashboards, or get a specific dashboard with its widgets by providing `dashboard_id`.
10. `thousandeyes_get_dashboard_widget(dashboard_id='ID', widget_id='WID', window='1h')` -- Get data for a specific widget. Requires both `dashboard_id` and `widget_id` from `thousandeyes_get_dashboards`.

### BGP Monitoring
11. `thousandeyes_get_bgp_monitors()` -- List BGP monitoring points. Shows monitor name, type, IP, network, and country. Use alongside BGP test results for routing analysis.

## RCA Investigation Workflow

**Step 1 -- Check active alerts:**
`thousandeyes_get_alerts(state='active')` -- identify active network alerts and their severity.

**Step 2 -- Check Internet Insights for macro outages:**
`thousandeyes_get_internet_insights(outage_type='network')` -- rule out ISP/transit-level outages.
`thousandeyes_get_internet_insights(outage_type='application')` -- rule out SaaS/CDN-level outages.

**Step 3 -- Discover relevant tests:**
`thousandeyes_list_tests()` -- find tests that monitor the affected service or network path.

**Step 4 -- Get test results:**
`thousandeyes_get_test_results(test_id='ID', result_type='network', window='1h')` -- check latency, loss, jitter.
`thousandeyes_get_test_results(test_id='ID', result_type='http', window='1h')` -- check HTTP availability.

**Step 5 -- Trace the network path:**
`thousandeyes_get_test_results(test_id='ID', result_type='path-vis')` -- hop-by-hop analysis to find where packets are dropping.

**Step 6 -- Understand alert rules:**
`thousandeyes_get_alert_rules()` -- review thresholds to understand why alerts fired.

**Step 7 -- Check agent health:**
`thousandeyes_get_agents()` -- verify monitoring agents are online and healthy.

**Step 8 -- BGP routing analysis:**
`thousandeyes_get_test_results(test_id='ID', result_type='bgp')` -- check for BGP route changes.
`thousandeyes_get_bgp_monitors()` -- list monitoring points for routing context.

## Important Rules
- ThousandEyes specializes in NETWORK-LAYER visibility. Use it for connectivity, latency, DNS, BGP, and internet outage investigations.
- Always start with `thousandeyes_get_alerts` and `thousandeyes_get_internet_insights` to get the broadest view.
- Use `thousandeyes_list_tests` to discover what is being monitored before querying results.
- The `window` parameter accepts strings like `'1h'`, `'6h'`, `'12h'`, `'1d'`. When omitted, results default to the latest test round.
- Results are truncated at 120,000 characters. Use filters to narrow results.
- Internet Insights outage data is particularly valuable for determining if an issue is localized or part of a broader internet event.
