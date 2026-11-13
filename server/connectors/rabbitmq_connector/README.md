# RabbitMQ connector

Read-only access to a broker through the [management HTTP API](https://www.rabbitmq.com/docs/management#http-api) (usually port 15672).

## Credentials

Create a user with the `monitoring` tag and read permission on each vhost Aurora should see. The tag can read the management API and cannot publish, purge, or change policy. It does not grant vhost access by itself. Connect lists the default vhost and refuses the account when that call is unauthorized.

| Field | Example |
|---|---|
| Management URL | `https://rabbitmq.internal.example.com:15672` |
| Username | `aurora-monitor` |
| Password | stored in Vault |
| Default vhost | `/` |
| Name (optional) | `production` |

The URL must be reachable from Aurora. Loopback and link-local addresses (including cloud metadata) are rejected. Private network addresses are allowed.

TLS verification follows `RABBITMQ_SSL_VERIFY` on the deployment: `false` (default), `true`, or a path to a CA bundle. The connect form does not choose this.

## Several brokers

Connect the form again with another management URL to add a broker. Each broker is its own account, keyed by its management `host:port`, and keeps its own user, password, and default vhost. Connecting the same URL again replaces that broker's credentials and keeps its name.

All brokers live in one Vault secret as an `accounts` list, and each gets a `user_connections` row so connector access can grant it to a role or team on its own. A secret saved before this list existed is read as one broker, and its row is backfilled on the next status check.

Agent tools take `account_id` (host:port, URL, or name). Without it, a call queries every broker the caller may use and returns `results_by_account`. `rabbitmq_list_brokers` lists them without calling RabbitMQ.

## What the agent can read

Cluster overview, node memory and disk alarms, queues (depth, consumers, dead-letter arguments, rates), exchanges, bindings, connections, consumers, and the built-in health checks.

Message bodies are not fetched. The management `get` endpoint dequeues messages, so it is not exposed.
