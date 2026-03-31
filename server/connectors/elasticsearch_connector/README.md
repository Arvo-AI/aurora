# Elasticsearch Connector

Connects Aurora to Elasticsearch (or OpenSearch) instances for log search, alerting, and root cause analysis.

## Features
- Connect via API key or basic auth (username/password)
- Query logs using Elasticsearch Query DSL
- Receive alert webhooks from Elasticsearch Watcher or OpenSearch Alerting
- Automatic RCA triggering on alert reception

## Configuration
Credentials are stored securely via the token management system (Vault-backed).

## Supported Versions
- Elasticsearch 7.x, 8.x
- OpenSearch 1.x, 2.x
