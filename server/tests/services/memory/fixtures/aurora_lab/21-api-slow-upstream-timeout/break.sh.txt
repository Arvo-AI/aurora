# An api release adds a stock-level lookup to every /items request. The call has
# no timeout, and the stock service's address drops packets (192.0.2.10 is
# TEST-NET-1, RFC 5737, never routed, standing in for a firewalled or retired
# dependency). Each /items request blocks in connect() until the kernel gives up,
# about 130 s later. /healthz does not call it, so the pods stay Ready and the
# Service keeps its endpoints. nginx in the frontend waits proxy_read_timeout
# (the default, 60 s) for a response header, then returns 504.
kubectl -n shop get cm api-code -o json | python3 -c '
import json, sys
cm = json.load(sys.stdin)
code = cm["data"]["api.py"]
old_import = "import json, os, socket, sys, time\n"
new_import = "import json, os, socket, sys, time, urllib.request\n"
old = """            redis_ping()
            self._send(200, {"items": ["kettle", "mug", "teapot"]})"""
new = """            redis_ping()
            try:
                with urllib.request.urlopen("http://192.0.2.10:8080/stock") as r:
                    stock = json.load(r)
            except OSError as e:
                print(f"stock lookup failed: {e}", flush=True)
                stock = {}
            self._send(200, {"items": ["kettle", "mug", "teapot"], "stock": stock})"""
assert old_import in code and old in code
cm["data"]["api.py"] = code.replace(old_import, new_import).replace(old, new)
for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields"):
    cm["metadata"].pop(k, None)
print(json.dumps(cm))' | kubectl apply -f -
kubectl -n shop rollout restart deploy/api
kubectl -n shop rollout status deploy/api --timeout=120s
