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
allowed-tools: rabbitmq_get_overview, rabbitmq_list_nodes, rabbitmq_list_queues, rabbitmq_get_queue, rabbitmq_list_exchanges, rabbitmq_list_bindings, rabbitmq_list_connections, rabbitmq_list_consumers, rabbitmq_health_checks
metadata:
  author: aurora
  version: "1.0"
---

# RabbitMQ Integration

Read-only management API. The connected user should have the `monitoring` tag. Do not try to publish, purge, delete, or fetch message bodies. Fetching bodies dequeues them, and these tools do not expose that endpoint.

## When a queue looks stuck

1. `rabbitmq_health_checks()` and `rabbitmq_list_nodes()` — memory or disk alarms block publishers cluster-wide.
2. `rabbitmq_get_overview()` — version, listeners, and whether ready or unacked messages are growing.
3. `rabbitmq_list_queues()` — deepest queues first. A queue with `consumers: 0` and a rising `messages_ready` has no worker. A high `messages_unacknowledged` means consumers are stuck or prefetch is too high.
4. `rabbitmq_get_queue(name='QUEUE')` — dead-letter arguments (`x-dead-letter-exchange`) and rates. Then inspect that dead-letter queue the same way.
5. `rabbitmq_list_consumers(vhost='VHOST')` and `rabbitmq_list_connections()` — confirm the worker process is still connected.
6. `rabbitmq_list_bindings(queue='QUEUE')` — no bindings means publishes never arrive.

## How to read the numbers

- `messages_ready` growing, `consumers` is 0: workers are down or subscribed to the wrong queue.
- `messages_unacknowledged` growing, consumers exist: the consumer is slow, blocked, or not acking.
- `consumer_utilisation` near 0 with consumers present: the consumer is idle or prefetch is exhausted.
- `mem_alarm` or `disk_free_alarm` true: the node is blocking publishers. This looks like a producer outage, not a consumer bug.
- `state` other than `running`: the queue is down or synchronising.
- A failed `amqp_listener` check is expected when the broker listens only on 5671, and the reverse.

Omit `vhost` on list calls to scan the cluster. Pass it once you know which vhost the failing service uses. The default saved at connect time is `/` unless the user set another.
