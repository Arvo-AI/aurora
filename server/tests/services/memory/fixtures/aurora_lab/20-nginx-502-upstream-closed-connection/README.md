# 502 Bad Gateway nginx: meaning and how to fix it

*nginx 502 Bad Gateway means the upstream sent no valid response. We reproduced 8 causes in Kubernetes; the error.log line tells you which one you hit.*

Runbook for lab scenario `20-nginx-502-upstream-closed-connection`. Canonical version, with its byline, kept current: https://www.aurorasre.ai/runbooks/fix-502-bad-gateway-nginx-kubernetes. Written from the lab runs in this repository; updated 2026-10-29.

## Reproduce it

```
./lab.sh up
./lab.sh break 20-nginx-502-upstream-closed-connection
```
Then investigate with any tool you like; `scenario.yaml` holds the ground truth and the grading criteria.

| Applies to | nginx as a reverse proxy (a frontend or sidecar pod) in front of HTTP upstreams; shown on Kubernetes with nginx proxying to a ClusterIP Service. The ingress-nginx controller runs the same nginx core but was not tested here |
|---|---|
| Verified on | Kubernetes server v1.37.0, kind v0.33.0, kubectl client v1.35.3, nginx 1.27-alpine (nginx/1.27.5), python:3.12-alpine (Python 3.12.16), redis 7.4.12, 2026-10-29 |
| Lab folder | [scenarios/20-nginx-502-upstream-closed-connection](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/20-nginx-502-upstream-closed-connection), transcripts in [runs/search-2026-10-29/20-nginx-502-upstream-closed-connection/canonical](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/search-2026-10-29/20-nginx-502-upstream-closed-connection/canonical) |

**A 502 Bad Gateway from nginx means nginx tried to pass the request to its upstream (the app or Kubernetes Service it proxies to) and did not get a valid HTTP response back: the connection was refused, closed or reset, or the reply could not be used. nginx writes the reason to its error log in the same second as the 502, so read that line before anything else: in Kubernetes, `kubectl logs <nginx-pod> | grep upstream`. `connect() failed (111: Connection refused)` points at the Service or port (a Service with no ready endpoints, or a `targetPort` with nothing listening), `upstream prematurely closed connection` points at the application itself, and `upstream timed out (110)` comes with a 504, not a 502 ([504 runbook](https://www.aurorasre.ai/runbooks/fix-504-gateway-timeout-nginx-kubernetes)).** We reproduced eight distinct 502 lines on nginx 1.27.5 in a kind cluster; the table below maps each one to its cause and the command that confirms it.

## At a glance

The first line is the only 502 reason that appeared anywhere in our lab's 38 captured cluster states for scenarios 01 to 19 (in 33 of them); the rest are ordered by how common the cause is in Kubernetes.

| error.log line (after the timestamp) | Status | Likely cause | Confirm with | Fix |
|---|---|---|---|---|
| `connect() failed (111: Connection refused) while connecting to upstream` | 502 | Service has no ready endpoints, wrong `targetPort`, or the app is not listening yet | `kubectl get endpointslices -l kubernetes.io/service-name=<svc>`, then `targetPort` against the container port | [Service with no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes), [targetPort mismatch](https://www.aurorasre.ai/runbooks/fix-service-targetport-mismatch-kubernetes) |
| `upstream prematurely closed connection while reading response header from upstream` | 502 | The app accepted the connection and closed it without answering: a crash inside the request handler, or a port that does not speak HTTP | `curl -v` to the Service from the nginx pod: `Empty reply from server`; then the app log | Fix the app (this page, scenario 20) or point nginx at the HTTP port |
| `recv() failed (104: Connection reset by peer) while reading response header from upstream` | 502 | The app reset the connection, or nginx sent plain HTTP to a TLS port | `curl -v` and `curl -vk https://` to the same port | Fix the app, or use `https://` in `proxy_pass` |
| `upstream sent too big header while reading response header from upstream` | 502 | Response headers (usually `Set-Cookie`) larger than `proxy_buffer_size` | `curl -sv -o /dev/null` to the upstream and measure the headers | Raise `proxy_buffer_size`, or send smaller headers |
| `SSL_do_handshake() failed (...) while SSL handshaking to upstream` | 502 | `proxy_pass https://` to a port that speaks plain HTTP | The Service port and what the container serves there | Use `http://`, or terminate TLS in the app |
| `no live upstreams while connecting to upstream` | 502 | Every server in an `upstream` group was marked failed, for example stale pod IPs from a headless Service | Compare the IPs in earlier error lines with the current EndpointSlice | Point at the ClusterIP Service, or add `zone` and `resolve` |
| `<name> could not be resolved (3: Host not found)` | 502 | `proxy_pass` with a variable and a `resolver`, and a name the resolver cannot answer (a typo, or a short Service name) | `nslookup <name>.<ns>.svc.cluster.local` from the pod | Use the full Service name |
| `no resolver defined to resolve <name>` | 502 | `proxy_pass` with a variable and no `resolver` directive | Look for `set $...` in front of `proxy_pass` | Add `resolver` with the kube-dns IP, or use a static name |
| `upstream timed out (110: Operation timed out) while connecting to upstream` | **504** | Packets dropped: a NetworkPolicy, a Service port that is not defined, a pod IP that no longer exists | `kubectl get networkpolicy`, the Service ports | [504 Gateway Timeout](https://www.aurorasre.ai/runbooks/fix-504-gateway-timeout-nginx-kubernetes), [NetworkPolicy blocking traffic](https://www.aurorasre.ai/runbooks/fix-networkpolicy-blocking-traffic-kubernetes) |
| `[emerg] host not found in upstream "<name>"` | none, nginx does not start | A static upstream name that does not resolve when nginx starts | `kubectl logs --previous` on the crashing pod | Create the Service or fix the name; see [nginx emerg errors](https://www.aurorasre.ai/runbooks/fix-nginx-emerg-unknown-directive-kubernetes) |

## What does 502 Bad Gateway mean?

HTTP defines it in [RFC 9110, section 15.6.3](https://www.rfc-editor.org/rfc/rfc9110.html#section-15.6.3): the 502 status code "indicates that the server, while acting as a gateway or proxy, received an invalid response from an inbound server it accessed while attempting to fulfill the request." The server that sends the 502 is working; the one behind it is not, or cannot be reached in a usable way. If you only see the page as a visitor, nothing on your side causes it, and waiting a minute and retrying is all you can do.

When the message reads `502 Bad Gateway` above `nginx` in small text, nginx generated the page itself. That matters for the next step: nginx always logs why. The neighbours are easy to confuse:

| Status | Who decided it | What nginx logs |
|---|---|---|
| 502 Bad Gateway | nginx: the upstream refused, closed, reset or sent something unusable | One of the eight `[error]` or `[crit]` lines in the table above |
| 503 Service Unavailable | Usually the upstream itself, passed through by nginx; nginx's own [`limit_req`](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html#limit_req_status) rejections also default to 503 | Nothing in error.log for a passed-through 503; only the access log line |
| 504 Gateway Timeout | nginx: no answer within `proxy_connect_timeout`, `proxy_send_timeout` or `proxy_read_timeout` | `upstream timed out (110: ...)`; the text in brackets depends on the C library, see the [504 runbook](https://www.aurorasre.ai/runbooks/fix-504-gateway-timeout-nginx-kubernetes) |

The 503 row is from our own lab: in scenarios 09 and 13 the api answered 503 itself when redis was unreachable, and the frontend's access log shows 503s with no error.log line next to them. A 5xx in the access log with no matching error.log line was produced by the upstream, not by nginx.

## How do you find the nginx error log for a 502 in Kubernetes?

The official nginx image links `/var/log/nginx/error.log` to `/dev/stderr` and the access log to `/dev/stdout` ([Dockerfile](https://github.com/nginx/docker-nginx/blob/master/mainline/alpine-slim/Dockerfile)), so both land in `kubectl logs`, interleaved. Each 502 in the access log has an error line at the same second. From scenario 20 (`diagnose.sh`, 2026-10-29 19:43 UTC):

```shell
kubectl -n shop logs pod/frontend-76d98597b9-g8l4n --tail=6
```

```
10.244.1.1 - - [29/Oct/2026:19:43:27 +0000] "GET /healthz HTTP/1.1" 200 3 "-" "kube-probe/1.37" "-"
10.244.1.1 - - [29/Oct/2026:19:43:32 +0000] "GET /healthz HTTP/1.1" 200 3 "-" "kube-probe/1.37" "-"
10.244.1.1 - - [29/Oct/2026:19:43:37 +0000] "GET /healthz HTTP/1.1" 200 3 "-" "kube-probe/1.37" "-"
10.244.1.13 - - [29/Oct/2026:19:43:39 +0000] "GET /api/items HTTP/1.1" 502 157 "-" "Wget" "-"
2026/10/29 19:43:39 [error] 26#26: *26 upstream prematurely closed connection while reading response header from upstream, client: 10.244.1.13, server: , request: "GET /api/items HTTP/1.1", upstream: "http://10.96.124.237:8080/items", host: "frontend"
10.244.1.1 - - [29/Oct/2026:19:43:42 +0000] "GET /healthz HTTP/1.1" 200 3 "-" "kube-probe/1.37" "-"
```

Read three fields: the reason (the text before `while`), the phase (`connecting to`, `reading response header from`, `SSL handshaking to`), and `upstream:`, the address nginx actually used. Here `10.96.124.237` is the api Service's ClusterIP, so nginx did reach a Service.

With more than a few pods, count the reasons instead of reading lines. This one command sorts every 502 and 504 cause in the last 500 lines of every nginx pod:

```shell
kubectl -n shop logs -l app=frontend --tail=500 | grep -oE '\[(error|crit|warn)\] .*while [a-z ]+ upstream' | sed -E 's/[0-9]+#[0-9]+: \*[0-9]+ //' | sort | uniq -c
```

```
 2 [error] connect() failed (111: Connection refused) while connecting to upstream
23 [error] upstream prematurely closed connection while reading response header from upstream
```

Two reasons, and they are not two problems. The two refusals are at 19:42:48 and 19:42:50, one per nginx pod, before the api pods were Ready; the client's first 200 came at 19:42:52, and every error since the change is `prematurely closed`. That startup pattern is the most common 502 in our lab (see below). Two reasons that both continue to the present usually mean two problems, or a cause that changed over time (a refused connection that becomes a timeout when the pod IP disappears). The resolver errors (`could not be resolved`, `no resolver defined`) do not contain `while ... upstream`; grep for `resolve` if you use variables in `proxy_pass`.

## Which nginx error.log line means which cause?

We built one nginx (the same `nginx:1.27-alpine` image as the lab frontend) with one `location` per failure, in front of a small backend that misbehaves differently on each port, and requested each location once. The manifest and the script are in the lab folder (`causes/`); every line below is from one run of it at 2026-10-29 19:40 UTC (`canonical/causes-transcript.txt`).

### connect() failed (111: Connection refused)

```
2026/10/29 19:40:24 [error] 26#26: *4 connect() failed (111: Connection refused) while connecting to upstream, client: 127.0.0.1, server: , request: "GET /noep/ HTTP/1.1", upstream: "http://10.96.180.255:8080/", host: "localhost"
2026/10/29 19:40:25 [error] 27#27: *6 connect() failed (111: Connection refused) while connecting to upstream, client: 127.0.0.1, server: , request: "GET /nolistener/ HTTP/1.1", upstream: "http://10.96.141.151:8090/", host: "localhost"
```

The first is a Service whose selector matches no pods; the second is a Service with endpoints whose `targetPort` (9999) has nothing listening. The lines are identical apart from the address, so the error log alone cannot separate them. The EndpointSlice can:

```shell
kubectl -n <ns> get endpointslices -l kubernetes.io/service-name=<svc>
```

`ENDPOINTS <unset>` is the [Service with no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes) case (selector or readiness). Pod IPs listed means the port is wrong: see [targetPort mismatch](https://www.aurorasre.ai/runbooks/fix-service-targetport-mismatch-kubernetes), or [a Service selecting the wrong pods](https://www.aurorasre.ai/runbooks/fix-service-selects-wrong-pods-label-overlap-kubernetes) when only some requests fail. A refusal is fast: the request fails in milliseconds, which is how you tell it from the 504 below.

### upstream prematurely closed connection

```
2026/10/29 19:40:35 [error] 31#31: *18 upstream prematurely closed connection while reading response header from upstream, client: 127.0.0.1, server: , request: "GET /close/ HTTP/1.1", upstream: "http://10.96.141.151:8081/", host: "localhost"
2026/10/29 19:40:43 [error] 25#25: *30 upstream prematurely closed connection while reading response header from upstream, client: 127.0.0.1, server: , request: "GET /redis/ HTTP/1.1", upstream: "http://10.96.95.254:6379/", host: "localhost"
```

The connection worked; the upstream hung up before sending a status line. The first is a backend that reads the request and closes the socket. The second is nginx pointed at the redis Service on 6379: redis is not HTTP and closes the connection. In Kubernetes this line means the network and the Service are fine and the problem is the process on the other end. Scenario 20, below, walks through it.

### recv() failed (104: Connection reset by peer)

```
2026/10/29 19:40:36 [error] 32#32: *20 recv() failed (104: Connection reset by peer) while reading response header from upstream, client: 127.0.0.1, server: , request: "GET /reset/ HTTP/1.1", upstream: "http://10.96.141.151:8082/", host: "localhost"
2026/10/29 19:40:38 [error] 25#25: *24 recv() failed (104: Connection reset by peer) while reading response header from upstream, client: 127.0.0.1, server: , request: "GET /plain-to-tls/ HTTP/1.1", upstream: "http://10.96.141.151:8084/", host: "localhost"
```

The first is a backend that aborts the connection (a TCP RST instead of a normal close). The second surprised us: `proxy_pass http://` to a port that speaks TLS logs a reset, with no mention of TLS. The TLS server rejected the plain-text request and reset the connection. If the Service port is named `https` or the app listens on 8443, check the scheme before blaming the app.

### upstream sent too big header

```
2026/10/29 19:40:37 [error] 34#34: *22 upstream sent too big header while reading response header from upstream, client: 127.0.0.1, server: , request: "GET /bighdr/ HTTP/1.1", upstream: "http://10.96.141.151:8083/", host: "localhost"
```

The backend answered `200` with a 16 KiB `Set-Cookie` header. nginx reads the response headers into one buffer of `proxy_buffer_size`, "4k|8k" by default, one memory page, and treats a larger header as invalid ([proxy module docs](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_buffer_size)). The app logs a successful request while the client gets a 502, which is why this one gets misread as a network problem. The fix is `proxy_buffer_size` large enough for the real headers (in the `location` or `server` block), or smaller cookies. We did not rerun this case with a larger buffer.

### SSL_do_handshake() failed

```
2026/10/29 19:40:39 [crit] 25#25: *26 SSL_do_handshake() failed (SSL: error:0A0000C6:SSL routines::packet length too long error:0A000139:SSL routines::record layer failure) while SSL handshaking to upstream, client: 127.0.0.1, server: , request: "GET /tls-to-plain/ HTTP/1.1", upstream: "https://10.96.70.88:8080/", host: "localhost"
```

`proxy_pass https://` to the lab api, which speaks plain HTTP on 8080. Note the level: `[crit]`, not `[error]`, so a grep for `\[error\]` misses it. The `upstream:` field starting with `https://` is the giveaway.

### no live upstreams

This one needs an `upstream` block with more than one server. nginx marks a server unavailable after `max_fails` failures (default 1) for `fail_timeout` (default 10 s); "If there is only a single server in a group, max_fails, fail_timeout and slow_start parameters are ignored, and such a server will never be considered unavailable" ([upstream module docs](https://nginx.org/en/docs/http/ngx_http_upstream_module.html#server)). A ClusterIP Service is one server, so you will not see this line in front of a normal Service.

The Kubernetes way to get it is a headless Service (`clusterIP: None`): nginx resolves the name once at startup, gets one A record per pod, and keeps those IPs. We restarted the backend Deployment; the EndpointSlice moved from `10.244.2.14 10.244.1.8` to `10.244.2.16 10.244.1.10`, and nginx kept trying the old addresses:

```
2026/10/29 19:41:41 [warn] 25#25: *44 upstream server temporarily disabled while connecting to upstream, client: 127.0.0.1, server: , request: "GET /headless/ HTTP/1.1", upstream: "http://10.244.2.14:8085/", host: "localhost"
2026/10/29 19:41:41 [error] 25#25: *44 upstream timed out (110: Operation timed out) while connecting to upstream, client: 127.0.0.1, server: , request: "GET /headless/ HTTP/1.1", upstream: "http://10.244.2.14:8085/", host: "localhost"
2026/10/29 19:41:43 [warn] 25#25: *44 upstream server temporarily disabled while connecting to upstream, client: 127.0.0.1, server: , request: "GET /headless/ HTTP/1.1", upstream: "http://10.244.1.8:8085/", host: "localhost"
2026/10/29 19:41:43 [error] 25#25: *44 upstream timed out (110: Operation timed out) while connecting to upstream, client: 127.0.0.1, server: , request: "GET /headless/ HTTP/1.1", upstream: "http://10.244.1.8:8085/", host: "localhost"
2026/10/29 19:41:44 [error] 25#25: *47 no live upstreams while connecting to upstream, client: 127.0.0.1, server: , request: "GET /headless/ HTTP/1.1", upstream: "http://headless/", host: "localhost"
```

The first request timed out against both dead IPs and returned 504; the next one, a second later, found both servers disabled and returned 502 without trying. The `upstream:` field changes to the group name (`http://headless/`) because no server was picked. The IPs in the earlier lines are the evidence: they are not in the current EndpointSlice.

Two things we did not expect. First, failure state is per nginx worker unless the group has a `zone`: two servers both refusing, requested twice, gave two full sets of `connect() failed` lines from two different worker PIDs (33 and 30) and no `no live upstreams` at all. Second, the fix works on open source nginx now: the `resolve` parameter on `server` re-resolves the name, needs a `zone` and a `resolver`, and was commercial-only before 1.27.3 (same docs page). The location using it returned 200 before and after the restart:

```
upstream headless_resolve {
  zone headless_resolve 64k;
  resolver 10.96.0.10 valid=10s;
  server backend-headless.r502x.svc.cluster.local:8085 resolve;
}
```

`10.96.0.10` is the kube-dns Service in kind; the docs accept the `resolver` "in the http block or in the corresponding upstream block". Simpler still: proxy to the ClusterIP Service instead of the headless one, and let kube-proxy follow the pods.

### could not be resolved (3: Host not found) and no resolver defined

Both come from `proxy_pass` with a variable (`set $u http://...; proxy_pass $u;`), which makes nginx resolve the name per request instead of at startup:

```
2026/10/29 19:40:46 [error] 25#25: *33 dynonly could not be resolved (3: Host not found), client: 127.0.0.1, server: , request: "GET /shortname/ HTTP/1.1", host: "localhost"
2026/10/29 19:40:48 [error] 25#25: *38 bakend.r502x.svc.cluster.local could not be resolved (3: Host not found), client: 127.0.0.1, server: , request: "GET /typo/ HTTP/1.1", host: "localhost"
```

and, for the location with no `resolver` in scope:

```
2026/10/29 19:40:45 [error] 25#25: *32 no resolver defined to resolve backend.r502x.svc.cluster.local, client: 127.0.0.1, server: , request: "GET /noresolver/ HTTP/1.1", host: "localhost"
```

The first line is the Kubernetes trap. `dynonly` is a real Service in the same namespace, and the same name with `.r502x.svc.cluster.local` returned 200. The pod's `/etc/resolv.conf` has `search r502x.svc.cluster.local svc.cluster.local cluster.local`, but in our run nginx's resolver looked up the name exactly as written and did not apply the search domains (the resolver docs do not mention search domains at all). Use the full `<svc>.<ns>.svc.cluster.local` name.

A second trap made the first version of this test wrong: the short name `backend` returned 200, because another `location` had a static `proxy_pass http://backend:8085/`. We reproduced that on its own in plain Docker with no `resolver` at all (`canonical/variable-proxy-pass-shadowing.txt`): `set $u http://backend:8085; proxy_pass $u;` returned 200 next to a static `proxy_pass http://backend:8085/`, and the same location with a name no static `proxy_pass` uses returned 502 with `no resolver defined to resolve other`. nginx checks names against the server groups it already knows before asking the resolver ("the name is searched among the described server groups, and, if not found, is determined using a resolver", [proxy_pass](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass)). A variable `proxy_pass` can work in one config and fail in another for that reason alone. For names that do not resolve at all, see [DNS name not resolving in Kubernetes](https://www.aurorasre.ai/runbooks/fix-dns-name-not-resolving-kubernetes).

### Not 502: upstream timed out (110), and host not found at startup

```
2026/10/29 19:40:28 [error] 29#29: *8 upstream timed out (110: Operation timed out) while connecting to upstream, client: 127.0.0.1, server: , request: "GET /unexposed/ HTTP/1.1", upstream: "http://10.96.141.151:9999/", host: "localhost"
2026/10/29 19:40:31 [error] 28#28: *10 upstream timed out (110: Operation timed out) while connecting to upstream, client: 127.0.0.1, server: , request: "GET /fenced/ HTTP/1.1", upstream: "http://10.96.63.157:8085/", host: "localhost"
2026/10/29 19:40:42 [error] 25#25: *28 upstream timed out (110: Operation timed out) while reading response header from upstream, client: 127.0.0.1, server: , request: "GET /slow/ HTTP/1.1", upstream: "http://10.96.141.151:8086/", host: "localhost"
```

All three returned 504. `/unexposed/` connects to the Service's ClusterIP on a port the Service does not define: the packets are dropped, not refused, so it is a timeout, which is the same thing our [ConfigMap drift](https://www.aurorasre.ai/runbooks/fix-configmap-change-not-applied-to-all-pods-kubernetes) scenario produced. `/fenced/` is a pod behind a deny-all NetworkPolicy. `/slow/` connected and then waited longer than `proxy_read_timeout`. The phase tells you which: `while connecting to` is the network, `while reading response header from` is a slow app. A wrong `targetPort` refuses (502); a wrong Service `port` times out (504). That asymmetry is worth remembering.

A name in a static `upstream` or `proxy_pass` that does not resolve stops nginx from starting at all, so there is no 502, only a crashing pod:

```
2026/10/29 19:41:46 [emerg] 178#178: host not found in upstream "noep-typo:8080" in /tmp/bad.conf:2
nginx: [emerg] host not found in upstream "noep-typo:8080" in /tmp/bad.conf:2
nginx: configuration file /tmp/nginx.conf test failed
```

That is the [nginx emerg](https://www.aurorasre.ai/runbooks/fix-nginx-emerg-unknown-directive-kubernetes) family: read `kubectl logs --previous`.

## How do you confirm an "upstream prematurely closed connection" 502 in Kubernetes?

Scenario 20 in the lab reproduces the case the other runbooks do not cover: every pod `Running` and `Ready`, the Service has endpoints, and every request still gets a 502. The change: the api's code (a ConfigMap) now reads `int(os.environ.get("ITEM_LIMIT"))` on each `/items` request, and the Deployment does not set `ITEM_LIMIT`. Every command and output below is from one run of `diagnose.sh` at 2026-10-29 19:43 UTC (`canonical/diagnose.txt`).

**Confirm.** The client sees 502 on every request:

```shell
kubectl -n shop logs deploy/loadgen --tail=5
```

```
2026-10-29T19:43:33Z GET /api/items -> 502
2026-10-29T19:43:35Z GET /api/items -> 502
2026-10-29T19:43:37Z GET /api/items -> 502
2026-10-29T19:43:39Z GET /api/items -> 502
2026-10-29T19:43:41Z GET /api/items -> 502
```

and the count above gave `upstream prematurely closed connection` 23 times, plus the two startup refusals. Every error since the change is a close, not a refusal, which rules out everything in the `connect()` family before you look at a Service.

**Scope.** Everything Kubernetes reports is healthy:

```shell
kubectl -n shop get pods -o wide
```

```
NAME                        READY   STATUS    RESTARTS   AGE   IP            NODE            NOMINATED NODE   READINESS GATES
api-7bb7ff87d4-pmkz5        1/1     Running   0          48s   10.244.2.21   r502b-worker2   <none>           <none>
api-7bb7ff87d4-tdnkm        1/1     Running   0          46s   10.244.1.14   r502b-worker    <none>           <none>
frontend-76d98597b9-g8l4n   1/1     Running   0          56s   10.244.1.12   r502b-worker    <none>           <none>
frontend-76d98597b9-qjvfm   1/1     Running   0          56s   10.244.2.20   r502b-worker2   <none>           <none>
loadgen-7c6676864-n2zfb     1/1     Running   0          56s   10.244.1.13   r502b-worker    <none>           <none>
redis-59458f55d8-jkblq      1/1     Running   0          56s   10.244.2.17   r502b-worker2   <none>           <none>
worker-6cf88d4b58-xm9j4     1/1     Running   0          56s   10.244.2.19   r502b-worker2   <none>           <none>
```

```shell
kubectl -n shop get endpointslices -l kubernetes.io/service-name=api
kubectl -n shop get svc api -o wide
```

```
NAME        ADDRESSTYPE   PORTS   ENDPOINTS                 AGE
api-9kqb6   IPv4          8080    10.244.2.21,10.244.1.14   56s

NAME   TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)    AGE   SELECTOR
api    ClusterIP   10.96.124.237   <none>        8080/TCP   56s   app=api
```

Both api pods are in the slice, the selector matches, the port is 8080. The api pods are 46 and 48 s old against 56 s for the rest: the api was rolled after everything else, which is the first hint of a change.

**Isolate.** Take nginx out of the path and ask the upstream yourself, from the nginx pod (the `nginx:alpine` image ships `curl`). The health endpoint answers:

```shell
kubectl -n shop exec pod/frontend-76d98597b9-g8l4n -c nginx -- curl -sS -o /dev/null -w '%{http_code}\n' --max-time 3 http://api:8080/healthz
```

```
200
```

The real route does not:

```shell
kubectl -n shop exec pod/frontend-76d98597b9-g8l4n -c nginx -- curl -sS -v --max-time 3 http://api:8080/items
```

```
* Host api:8080 was resolved.
* IPv6: (none)
* IPv4: 10.96.124.237
*   Trying 10.96.124.237:8080...
* Connected to api (10.96.124.237) port 8080
* using HTTP/1.x
> GET /items HTTP/1.1
> Host: api:8080
> User-Agent: curl/8.12.1
> Accept: */*
> 
* Request completely sent off
* Empty reply from server
* shutting down connection #0
curl: (52) Empty reply from server
command terminated with exit code 52
```

`Connected` and then `Empty reply from server` is curl's way of saying what nginx said. The same request to a pod IP (`http://10.244.2.21:8080/items`) gave the same `curl: (52) Empty reply from server`, which takes kube-proxy out of the picture too. Now the app's own log:

```shell
kubectl -n shop logs -l app=api --tail=16
```

```
Traceback (most recent call last):
File "/usr/local/lib/python3.12/socketserver.py", line 697, in process_request_thread
  self.finish_request(request, client_address)
File "/usr/local/lib/python3.12/socketserver.py", line 362, in finish_request
  self.RequestHandlerClass(request, client_address, self)
File "/usr/local/lib/python3.12/socketserver.py", line 766, in __init__
  self.handle()
File "/usr/local/lib/python3.12/http/server.py", line 440, in handle
  self.handle_one_request()
File "/usr/local/lib/python3.12/http/server.py", line 428, in handle_one_request
  method()
File "/code/api.py", line 29, in do_GET
  limit = int(os.environ.get("ITEM_LIMIT"))
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
TypeError: int() argument must be a string, a bytes-like object or a real number, not 'NoneType'
----------------------------------------
```

(trimmed: the second pod's 16 lines, the tail of the same traceback and two `/healthz` 200 lines). In the full log each failed request leaves a block that starts `Exception occurred during processing of request from ('10.244.2.20', 55958)`; `10.244.2.20` is a frontend pod, so the client the api failed is nginx. Python's `http.server` catches the exception, prints it and closes the socket without writing a response, which is exactly the close nginx reported. The handler's `except OSError` exists for redis errors and does not catch a `TypeError`. An app that caught the exception and answered 500 would have produced a passed-through 500 with a log line; an uncaught exception, a crashing worker process or a segfault produces this 502 instead.

**After which change.** The ReplicaSets and the diff against source control:

```shell
kubectl -n shop get rs -l app=api -o wide
```

```
NAME             DESIRED   CURRENT   READY   AGE   CONTAINERS   IMAGES               SELECTOR
api-7bb7ff87d4   2         2         2       48s   api          python:3.12-alpine   app=api,pod-template-hash=7bb7ff87d4
api-f64486c4c    0         0         0       56s   api          python:3.12-alpine   app=api,pod-template-hash=f64486c4c
```

```shell
kubectl -n shop get deploy api -o jsonpath='{.spec.template.spec.containers[0].env}'
kubectl diff -f app/shop.yaml
```

```
[{"name":"REDIS_HOST","value":"redis"}]
```

```
@@ -29,9 +29,8 @@
           if self.path == "/healthz":
               return self._send(200, {"status": "ok"})
           try:
-                limit = int(os.environ.get("ITEM_LIMIT"))
               redis_ping()
-                self._send(200, {"items": ["kettle", "mug", "teapot"][:limit]})
+                self._send(200, {"items": ["kettle", "mug", "teapot"]})
           except OSError as e:
               print(f"request failed: redis error: {e}", flush=True)
               self._send(503, {"error": "backend unavailable"})
```

(trimmed: the diff's file header lines). The same image on both ReplicaSets, so the image did not change; the env has no `ITEM_LIMIT`; and `kubectl diff`, a server-side dry run that changes nothing, shows the live ConfigMap differs from the manifest by exactly the new line. `kubectl rollout history deploy/api` only listed revisions 1 and 2 with `CHANGE-CAUSE <none>`, which says a rollout happened and nothing about why.

## How do you fix a 502 from nginx?

Fix the cause the log line names, not nginx. For scenario 20, put the previous api code back and roll the api so the pods load it:

```shell
kubectl apply -f <the api-code ConfigMap from source control>
kubectl -n shop rollout restart deploy/api
kubectl -n shop rollout status deploy/api --timeout=120s
```

```
configmap/api-code configured
deployment.apps/api restarted
deployment "api" successfully rolled out
```

(trimmed: five `Waiting for deployment "api" rollout to finish` lines; `fix.sh` in the lab folder does these three steps, taking the ConfigMap from `app/shop.yaml`.) Risk: low. The restart replaces the api pods one at a time, and they were failing every real request anyway. Rollback: apply the newer ConfigMap again and restart. The forward fix is the one in the code: give `ITEM_LIMIT` a default, or set it on the Deployment (`kubectl -n shop set env deploy/api ITEM_LIMIT=3` rolls the pods), and ship both together next time.

The mitigation that looks right does not work here:

```shell
kubectl -n shop rollout undo deploy/api
```

```
deployment.apps/api rolled back
```

Twenty seconds after the undo finished, the client still logged `GET /api/items -> 502` on every request, and the two nginx pods had logged 7 `prematurely closed` lines in the last 15 s. `rollout undo` restores the previous pod template; the code lives in a ConfigMap, which no Deployment revision records, so the "old" pods mount the new code. When the release is a ConfigMap, the rollback is the ConfigMap.

For the other lines in the table, the fix belongs to the cause: the Service selector or `targetPort` for `111`, the `proxy_pass` scheme for the TLS lines, `proxy_buffer_size` for big headers, the full Service name for resolver errors, and a ClusterIP Service (or `zone` plus `resolve`) for `no live upstreams`. Restarting nginx changes nothing in any of them except the headless case, where it re-resolves the names once and will fail again at the next rollout.

## How do you verify the 502 is fixed?

Pass condition: the last ten client requests are all 200 and no nginx pod logged the reason line in the last 30 s. `verify.sh` in the lab folder checks both and exits non-zero otherwise:

```shell
kubectl -n shop logs deploy/loadgen --tail=10
kubectl -n shop logs -l app=frontend --since=30s --tail=-1 | grep -c 'upstream prematurely closed connection'
```

After the fix (19:44 UTC, `canonical/fix-verify.txt`):

```
2026-10-29T19:44:49Z GET /api/items -> 200
2026-10-29T19:44:51Z GET /api/items -> 200
2026-10-29T19:44:53Z GET /api/items -> 200
2026-10-29T19:44:55Z GET /api/items -> 200
2026-10-29T19:44:57Z GET /api/items -> 200
2026-10-29T19:44:59Z GET /api/items -> 200
2026-10-29T19:45:01Z GET /api/items -> 200
2026-10-29T19:45:03Z GET /api/items -> 200
2026-10-29T19:45:05Z GET /api/items -> 200
2026-10-29T19:45:07Z GET /api/items -> 200
prematurely closed lines in the last 30 s: 0
verify exit code: 0
```

Against the broken state the same script printed `prematurely closed lines in the last 30 s: 15` and exited 1. Pass `--tail=-1`: with `-l`, `kubectl logs` returns only the last 10 lines per pod (`kubectl logs --help`: "Defaults to -1 with no selector, showing all log lines otherwise 10, if a selector is provided"). Keep watching for one `fail_timeout` (10 s by default) after a fix that involves an `upstream` group, so disabled servers come back.

## How do you prevent nginx 502s?

**Probe the real path, not only `/healthz`.** In scenario 20 the readiness probe passed on every pod while every real request failed, because `/healthz` returns before the broken line. A post-deploy smoke test that calls one real route (`curl -fsS http://api:8080/items`) and fails the rollout would have stopped it.

**Alert on the reason, not only the status.** If nginx logs reach Loki, count error lines by reason so the page names the cause (LogQL; we have not run this query in the lab):

```
sum by (reason) (count_over_time({namespace="shop", container="nginx"} |~ "while (connecting to|reading response header from|SSL handshaking to) upstream" | regexp `\*\d+ (?P<reason>[^,]+?) while` [5m]))
```

Alert on any non-zero series. A rising `connect() failed (111` points at Kubernetes objects, a rising `prematurely closed` at the last deploy of the app. For the `111` family, the endpoints alert in the [no endpoints runbook](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes) fires before the first 502.

**Guardrails.** Keep config and code ConfigMaps in source control and run `kubectl diff` in CI, as above. Point nginx at ClusterIP Services, not headless ones, unless you add `zone` and `resolve`. Use full Service names in any variable `proxy_pass`.

## Common mistakes

- **Treating every 502 as a networking problem.** `prematurely closed`, `104` and `too big header` all mean the connection worked. NetworkPolicy, DNS and Service checks will all pass.
- **Grepping only `[error]`.** The TLS handshake failure logs at `[crit]`, and the headless failures start with `[warn] upstream server temporarily disabled`.
- **Reading the 503s and 504s as the same incident.** A 503 with no error line is the app's own answer; a 504 is a timeout with a different cause list.
- **`kubectl rollout undo` when the change was a ConfigMap.** We ran it: still 502.
- **Restarting nginx.** It re-reads config and re-resolves names, so it can hide a headless-Service problem until the next rollout and fixes nothing else on this list.
- **Raising `proxy_connect_timeout` or `proxy_read_timeout` for a 502.** Timeouts produce 504s. A 502 never waited long enough for a timeout to matter.

## What we saw in the lab

The lab's frontend is nginx proxying `/api/` to the api Service, and the runner captures every pod's log before any investigation starts. We counted every line in the 82 frontend log files of the 38 captured states of scenarios 01 to 19 (runs r1 and r2 of each; 01 to 10 from `v0-run2`, 11 to 19 from `v1-run1`, all captured 2026-10-02; each log is complete from container start). Only two upstream error reasons appear: `connect() failed (111: Connection refused)` (184 lines) and `upstream timed out (110)` (15 lines). The only other line above `[notice]` is scenario 05's `[emerg] unknown directive`, where nginx did not start. `connect() failed (111: Connection refused)` appeared in 33 of the 38 states. In 28 of those it was 1 to 3 lines, the last one 2 to 4 s before the client's first 200: startup, before the api pods were Ready. In 02 r1 (a sidecar OOMKilled inside the api pods) there were 11, 9 of them after the first 200, as the pods dropped out of the endpoints. Only scenario 07 (selector matches no pods) and scenario 11 (wrong `targetPort`) produced a sustained stream, 23 to 26 lines per run, one per request. `upstream timed out (110)` appeared only in scenario 19, 9 and 6 times, where the edited ConfigMap pointed one nginx pod at a Service port that does not exist, and those requests returned 504.

Two observations from those runs belong on this page. Scenario 12's client logged `timeout` for 17 and 18 requests (r1 and r2) with no nginx error line for any of them: the frontend Service itself had no endpoints, so requests never reached nginx, and nginx's silence was the clue. And scenarios 09 (30 and 31 client 503s) and 13 (13 in each run) produced 503s that nginx passed through without an error line, because the api answered 503 itself.

So in our lab, the startup race is the most common source of a 502 by count, and it is not a fault. Before chasing a 502, check whether it stopped at the first `-> 200`.

Scenario 20 is new in this batch and adds the first cause where Kubernetes reports nothing wrong. `./lab.sh selftest 20-nginx-502-upstream-closed-connection` passed on our cluster (`broken 20-nginx-502-upstream-closed-connection after 0s`, `selftest: 1 passed, 0 failed`), and the fault check requires all three: a 502 at the client, the `prematurely closed` line in nginx, and a non-empty EndpointSlice. The Aurora investigation for this scenario has not been run yet, so there is no Aurora trace or verdict for it on this page. Aurora's results on scenarios 07 and 11, the `111` cases, are on the [benchmark](https://www.aurorasre.ai/benchmark) and in the [no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes) and [targetPort](https://www.aurorasre.ai/runbooks/fix-service-targetport-mismatch-kubernetes) runbooks.

The cause experiment also corrected two of our assumptions: a dropped connection (NetworkPolicy, wrong Service port) is a 504, not a 502, and plain HTTP to a TLS port shows up as a reset, not a TLS error. The 504 side, slow upstreams and timeouts, is covered in the [504 Gateway Timeout runbook](https://www.aurorasre.ai/runbooks/fix-504-gateway-timeout-nginx-kubernetes).

## How Aurora handles this

Aurora receives the alert and investigates with read-only kubectl under its default `observability_only` command policy, choosing the commands itself: pod and Service state, EndpointSlices, and the logs of nginx and the app behind it. It records every tool call and its output and cites them in the report. The policy blocks mutating commands, and it blocked `exec` in three of our graded lab runs (16 r1, 19 r1 and 19 r2), so it may not be able to run the `curl` from inside the nginx pod shown here and would then have to work from the logs, which on this page are enough on their own. It proposes a fix rather than applying it. Aurora has not yet been run on scenario 20.

- **GitHub:** [github.com/Arvo-AI/aurora](https://github.com/Arvo-AI/aurora)
- **Book a demo:** [cal.com/arvo-ai/demo](https://cal.com/arvo-ai/demo)

Related: [504 Gateway Timeout in nginx](https://www.aurorasre.ai/runbooks/fix-504-gateway-timeout-nginx-kubernetes), [Service has no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes), [Service targetPort mismatch](https://www.aurorasre.ai/runbooks/fix-service-targetport-mismatch-kubernetes), [Service selecting the wrong pods](https://www.aurorasre.ai/runbooks/fix-service-selects-wrong-pods-label-overlap-kubernetes), [NetworkPolicy blocking traffic](https://www.aurorasre.ai/runbooks/fix-networkpolicy-blocking-traffic-kubernetes), [nginx emerg unknown directive](https://www.aurorasre.ai/runbooks/fix-nginx-emerg-unknown-directive-kubernetes), [ConfigMap change reached only some pods](https://www.aurorasre.ai/runbooks/fix-configmap-change-not-applied-to-all-pods-kubernetes), [DNS name not resolving](https://www.aurorasre.ai/runbooks/fix-dns-name-not-resolving-kubernetes), and [root cause analysis for SREs](https://www.aurorasre.ai/blog/root-cause-analysis-complete-guide-sres).

## Sources and verification

- Scenario 20 commands and outputs: `selftest.txt`, `break.txt`, `diagnose.txt`, `evidence-before/`, `mistake-rollout-undo.txt`, `verify-broken.txt` and `fix-verify.txt` in `runs/search-2026-10-29/20-nginx-502-upstream-closed-connection/canonical`, captured 2026-10-29 between 19:38 and 19:48 UTC on a fresh three-node kind cluster in one sitting. The fault is injected by `scenarios/20-nginx-502-upstream-closed-connection/break.sh`. Earlier drafts' captures from 15:48 to 16:29 UTC are kept beside `canonical/` and are not quoted here.
- The eight 502 lines and the 504 contrasts: `causes/causes.yaml`, `causes/backend.py` and `causes/run.sh` in the scenario folder; output in `canonical/causes-transcript.txt` (one run, 19:40 to 19:41 UTC, every case). The proxy pod's `/etc/resolv.conf` and the version checks are in `canonical/extra-checks.txt`.
- Counts across scenarios 01 to 19: the frontend and loadgen logs in `evidence-before/logs/` of `runs/v0-run2` (01 to 10) and `runs/v1-run1` (11 to 19), r1 and r2 of each scenario, 38 states and 82 frontend log files.
- Versions: Kubernetes server v1.37.0, kind v0.33.0, kubectl client v1.35.3 (`kubectl version`, `kind version`), nginx/1.27.5 (`nginx -v` in the pod), python:3.12-alpine (Python 3.12.16), redis 7.4.12, curl 8.12.1. The nginx error strings and the status each one returns were checked against the nginx 1.27.5 source (`src/http/ngx_http_upstream.c`, `ngx_http_upstream_round_robin.c`).
- nginx documentation, read 2026-10-29: [ngx_http_upstream_module](https://nginx.org/en/docs/http/ngx_http_upstream_module.html) (`max_fails`, `fail_timeout`, single-server rule, `zone`, `resolve` and its 1.27.3 note), [ngx_http_proxy_module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html) (`proxy_buffer_size`, variables in `proxy_pass`), [ngx_http_core_module resolver](https://nginx.org/en/docs/http/ngx_http_core_module.html#resolver). The proxy docs note that `proxy_http_version` defaults to 1.1 from nginx 1.29.7; our 1.27.5 used 1.0, which did not change any line above.
- [RFC 9110 section 15.6](https://www.rfc-editor.org/rfc/rfc9110.html#section-15.6) for 502, 503 and 504, and the official image's [Dockerfile](https://github.com/nginx/docker-nginx/blob/master/mainline/alpine-slim/Dockerfile) for the log symlinks, read 2026-10-29.

## Questions people ask

**What does 502 Bad Gateway mean?**

A server acting as a gateway or proxy, such as nginx, got an invalid response from the server behind it, or could not get one at all: the connection was refused, closed or reset, or the reply was unusable. RFC 9110 defines it in section 15.6.3. The proxy itself is working; the upstream is the problem.

**How do I fix a 502 Bad Gateway in nginx?**

Read the nginx error log line written in the same second as the 502 (kubectl logs on the nginx pod in Kubernetes). connect() failed (111: Connection refused) means fix the Service or port, upstream prematurely closed connection means fix the application, upstream sent too big header means raise proxy_buffer_size, and the resolver errors mean fix the name in proxy_pass. Restarting nginx fixes none of these.

**What causes upstream prematurely closed connection while reading response header from upstream?**

The upstream accepted the connection and closed it before sending a status line. In our lab that was an uncaught exception in a Python request handler, and separately nginx pointed at a redis port, which does not speak HTTP. Confirm with curl -v to the Service from the nginx pod: Connected, then Empty reply from server.

**Why does nginx return 502 when all my Kubernetes pods are Running and Ready?**

Readiness probes usually hit a health endpoint, not the real route. In our scenario 20 /healthz returned 200 on every pod while every /items request crashed the handler, so the pods stayed Ready, the Service kept its endpoints, and nginx returned 502. Call the real route with curl from the nginx pod to see it.

**Why does a wrong targetPort give a 502 but a wrong Service port a 504?**

In our kind cluster a Service whose targetPort has nothing listening behind it refused the connection at once, so nginx logged connect() failed (111: Connection refused) and returned 502. A request to a port the Service does not define was dropped, not refused, so nginx waited for proxy_connect_timeout, logged upstream timed out (110) while connecting to upstream and returned 504. A NetworkPolicy that blocks the pod behaves the same way: 504. The 504 runbook covers timeouts in depth.

**Why does nginx say no live upstreams?**

Every server in an upstream block with more than one server was marked unavailable after failing, max_fails (1) times within fail_timeout (10 s) by default. In Kubernetes this happens when nginx proxies to a headless Service, resolves the pod IPs once at startup and keeps them after a rollout. Proxy to the ClusterIP Service instead, or add zone and the resolve parameter (open source since nginx 1.27.3).

**Is a 502 Bad Gateway error my fault?**

Not if you are a visitor. A 502 means one server could not get a valid answer from another server behind it, so nothing on your device causes it. Waiting a minute and retrying is all a visitor can do; the site operator has to fix the upstream.


---
This file mirrors https://www.aurorasre.ai/runbooks/fix-502-bad-gateway-nginx-kubernetes; edits land there first. Benchmark of three agents on this and 18 other faults: https://www.aurorasre.ai/benchmark
