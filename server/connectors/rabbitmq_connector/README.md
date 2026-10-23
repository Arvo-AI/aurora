# RabbitMQ connector

Read-only access to a broker through the [management HTTP API](https://www.rabbitmq.com/docs/management#http-api) (usually port 15672).

## Credentials

Create a user with the `monitoring` tag. That tag can read queues, nodes, connections, and alarms, and cannot publish, purge, or change policy.

| Field | Example |
|---|---|
| Management URL | `https://rabbitmq.internal.example.com:15672` |
| Username | `aurora-monitor` |
| Password | stored in Vault |
| Default vhost | `/` |

The URL must be reachable from Aurora. Loopback and link-local addresses (including cloud metadata) are rejected. Private network addresses are allowed.

TLS verification is on unless the connect form disables it for a private CA.

## What the agent can read

Cluster overview, node memory and disk alarms, queues (depth, consumers, dead-letter arguments, rates), exchanges, bindings, connections, consumers, and the built-in health checks.

Message bodies are not fetched. The management `get` endpoint dequeues messages, so it is not exposed.
