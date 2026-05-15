---
name: cloudflare
id: cloudflare
description: "Cloudflare integration for DNS, CDN, WAF, edge diagnostics, and remediation with zone management, analytics, security events, and cache control"
category: cloud_provider
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.cloudflare_tool
  function: is_cloudflare_connected
tools:
  - query_cloudflare
  - cloudflare_list_zones
  - cloudflare_action
index: "Cloudflare — DNS, CDN, WAF, analytics, security events, cache/firewall remediation"
rca_priority: 10
allowed-tools: query_cloudflare, cloudflare_list_zones, cloudflare_action
metadata:
  author: aurora
  version: "1.0"
---

# Cloudflare Integration

## Overview
Cloudflare is connected for DNS, CDN, WAF, and edge diagnostics with full remediation capabilities.

## Instructions

### IMPORTANT -- NO CLI SUPPORT
- Do NOT use `cloud_exec('cloudflare', ...)` -- there is no Cloudflare CLI connector.
- Use the dedicated `query_cloudflare`, `cloudflare_list_zones`, and `cloudflare_action` tools instead.

### OBSERVATION TOOLS (read-only)
- **List zones**: `cloudflare_list_zones()` -- discover all zones with IDs, names, and status.
- **DNS records**: `query_cloudflare(resource_type='dns_records', zone_id='...')` -- list A, AAAA, CNAME, MX, TXT records.
- **Analytics**: `query_cloudflare(resource_type='analytics', zone_id='...')` -- traffic, bandwidth, threats, HTTP status codes, content types, HTTP versions, SSL protocols, IP classification.
  - Pass `since` (e.g. '-60' for last hour, or ISO-8601) and `until` (ISO-8601) to control the time window.
  - Bucket granularity is auto-selected: minute buckets for <=100 min, hourly for <=100 h, daily beyond that.
  - Default limit=50 returns a bucketed time-series (e.g., last 24h yields multiple hourly buckets). Set `limit=1` to force a single aggregate covering the entire window.
- **Security events**: `query_cloudflare(resource_type='firewall_events', zone_id='...')` -- recent WAF blocks, challenges, JS challenges.
- **Firewall rules**: `query_cloudflare(resource_type='firewall_rules', zone_id='...')` -- active firewall rules and expressions.
- **Rate limits**: `query_cloudflare(resource_type='rate_limits', zone_id='...')` -- rate limiting rules (thresholds, actions, URL patterns).
- **Zone settings**: `query_cloudflare(resource_type='zone_settings', zone_id='...')` -- ALL zone settings (security level, caching, dev mode, WAF, TLS version, minification, etc.).
- **Page rules**: `query_cloudflare(resource_type='page_rules', zone_id='...')` -- URL-based redirects, forwarding, cache overrides.
- **Workers**: `query_cloudflare(resource_type='workers')` -- list Cloudflare Workers scripts.
- **Load balancers**: `query_cloudflare(resource_type='load_balancers', zone_id='...')` -- LB config, pools, failover.
- **SSL/TLS**: `query_cloudflare(resource_type='ssl', zone_id='...')` -- TLS mode (off/flexible/full/strict) and cert status.
- **Healthchecks**: `query_cloudflare(resource_type='healthchecks', zone_id='...')` -- origin health monitors.

### REMEDIATION TOOLS (write actions via `cloudflare_action`)
All remediation uses one tool: `cloudflare_action(action_type='...', zone_id='...', ...)`

- **Purge cache**: `cloudflare_action(action_type='purge_cache', zone_id='...', files=['https://...'])` -- clear cached content.
  - Omit `files` to purge everything (use with caution -- spikes origin load).
- **Under Attack Mode**: `cloudflare_action(action_type='security_level', zone_id='...', value='under_attack')` -- enable JS challenge for all visitors.
  - Other values: 'high', 'medium', 'low', 'essentially_off'.
  - Use during active DDoS or abuse. Remember to lower it after the incident.
- **Development mode**: `cloudflare_action(action_type='development_mode', zone_id='...', value='on')` -- bypass cache entirely.
  - Useful for debugging stale content issues. Auto-expires after 3 hours.
- **DNS update**: `cloudflare_action(action_type='dns_update', zone_id='...', record_id='...', content='1.2.3.4')` -- change a DNS record.
  - Use for failover to backup origin, maintenance page, or IP migration.
  - Get record_id from `query_cloudflare(resource_type='dns_records')`.
  - Also supports `proxied` (bool) and `ttl` (int, 1=auto).
- **Toggle firewall rule**: `cloudflare_action(action_type='toggle_firewall_rule', zone_id='...', rule_id='...', paused=True)` -- disable a rule.
  - Use to unblock false-positive blocks or emergency-enable a blocking rule.
  - Get rule_id from `query_cloudflare(resource_type='firewall_rules')`.

### RCA WORKFLOW
1. Start with `cloudflare_list_zones()` to discover zone IDs.
2. Check `zone_settings` for current security level, dev mode, caching config.
3. Check `analytics` for traffic spikes, elevated error rates (5xx), or threat surges.
4. Check `firewall_events` if traffic is being blocked unexpectedly.
5. Check `firewall_rules` and `rate_limits` if legitimate traffic appears throttled.
6. Check `dns_records` if a domain resolution issue is suspected.
7. Check `ssl` if TLS handshake errors are reported.
8. Check `healthchecks` and `load_balancers` if origin availability is degraded.
9. Check `page_rules` if redirects or caching overrides are misbehaving.

### CRITICAL RULES
- NEVER call cloud_exec with provider='cloudflare' -- it will fail.
- NEVER use query_cloudflare to list zones -- use `cloudflare_list_zones()` instead.
- Always get zone IDs first before querying zone-specific data.
- Only zones enabled by the user are accessible; others will be rejected.
- Analytics covers the last 24h by default; use the `since` parameter for custom ranges.
- Remediation actions require write permissions on the token; if a 403 is returned, tell the user which permission to add.
