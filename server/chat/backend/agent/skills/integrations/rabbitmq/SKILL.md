---
name: rabbitmq
id: rabbitmq
description: "RabbitMQ management API for queue depth, consumers, node alarms, connections, and broker health during root cause analysis"
category: infrastructure
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.rabbitmq_tool
  function: is_rabbitmq_connected
tools:
  - rabbitmq_list_brokers
  - rabbitmq_get_overview
  - rabbitmq_list_nodes
  - rabbitmq_list_queues
  - rabbitmq_get_queue
  - rabbitmq_list_exchanges
  - rabbitmq_list_bindings
  - rabbitmq_list_connections
  - rabbitmq_list_consumers
  - rabbitmq_health_checks
index: "RabbitMQ broker health -- queues, consumers, nodes, alarms, connections, bindings"
rca_priority: 8
allowed-tools: rabbitmq_list_brokers, rabbitmq_get_overview, rabbitmq_list_nodes, rabbitmq_list_queues, rabbitmq_get_queue, rabbitmq_list_exchanges, rabbitmq_list_bindings, rabbitmq_list_connections, rabbitmq_list_consumers, rabbitmq_health_checks
metadata:
  author: aurora
  version: "1.1"
---

# RabbitMQ Integration

Read-only management API. The connected user should have the `monitoring` tag and read permission on each vhost. The tag does not grant that permission. Do not try to publish, purge, delete, or fetch message bodies. Fetching bodies dequeues them, and these tools do not expose that endpoint.

## Several brokers

More than one broker can be connected. Each is its own account, identified by its management `host:port` (for example `rabbit-prod.internal:15672`), and may also have a saved name. A role or team may be allowed one broker and not another. Tools only see brokers this user can access.

- `rabbitmq_list_brokers()` lists them with `account_id`, `broker` (name), and the default vhost. It makes no call to RabbitMQ.
- Omit `account_id` to query every allowed broker. With more than one, the result is `results_by_account`, keyed by account id; one broker's error does not hide the others.
- Pass `account_id='host:port'` (or the saved name) once you know which broker the failing service uses, and keep passing it.
- Every result starts with `brokers`: the brokers that call talked to. Say which broker a finding came from.
- Queue and vhost names can repeat across brokers. `rabbitmq_get_queue` and `rabbitmq_list_bindings` without `account_id` return a not-found error on brokers that lack the queue; that is expected.

## When a queue looks stuck

1. `rabbitmq_health_checks()` and `rabbitmq_list_nodes()` — memory or disk alarms block publishers cluster-wide.
2. `rabbitmq_get_overview()` — version, listeners, and whether ready or unacked messages are growing.
3. `rabbitmq_list_queues()` — deepest queues this user can read. Omitting `vhost` does not show queues the user has no permission on. A queue with `consumers: 0` and a rising `messages_ready` has no subscriber on that queue. Confirm the worker connection before calling it down. A high `messages_unacknowledged` means consumers are stuck or prefetch is too high.
4. `rabbitmq_get_queue(name='QUEUE')` — dead-letter arguments (`x-dead-letter-exchange`) and rates. Then inspect that dead-letter queue the same way.
5. `rabbitmq_list_consumers(vhost='VHOST')` and `rabbitmq_list_connections()` — confirm the worker process is still connected.
6. `rabbitmq_list_bindings(queue='QUEUE')` — custom bindings. An empty list is not proof of dropped publishes: the default exchange routes to a queue by its name and does not create a binding row.

## How to read the numbers

- `messages_ready` growing, `consumers` is 0: workers are down or subscribed to the wrong queue.
- `messages_unacknowledged` growing, consumers exist: the consumer is slow, blocked, or not acking.
- `consumer_utilisation` near 0 with consumers present: the consumer is idle or prefetch is exhausted.
- `mem_alarm` or `disk_free_alarm` true: the node is blocking publishers. This looks like a producer outage, not a consumer bug.
- `state` other than `running`: the queue is down or synchronising.
- A failed `amqp_listener` check is expected when the broker listens only on 5671, and the reverse.
- `list_queues` or `list_exchanges` is empty while `object_totals` is not: this user has no read permission on those vhosts. The monitoring tag does not grant it. A 401 that says the user cannot read that vhost is the same gap, not a bad password.

Omit `vhost` to list every vhost this user can read. Pass it once you know which vhost the failing service uses. Each broker keeps its own default vhost from connect time, `/` unless the user set another.
