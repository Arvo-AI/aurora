# An api release reads a new setting, ITEM_LIMIT, on every /items request. The
# Deployment was not given the variable, so int(None) raises inside the request
# handler. Python's http.server then closes the connection without writing a
# response; /healthz returns before the new line, so the pods stay Ready.
kubectl -n shop get cm api-code -o json | python3 -c '
import json, sys
cm = json.load(sys.stdin)
old = """            redis_ping()
            self._send(200, {"items": ["kettle", "mug", "teapot"]})"""
new = """            limit = int(os.environ.get("ITEM_LIMIT"))
            redis_ping()
            self._send(200, {"items": ["kettle", "mug", "teapot"][:limit]})"""
assert old in cm["data"]["api.py"]
cm["data"]["api.py"] = cm["data"]["api.py"].replace(old, new)
for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields"):
    cm["metadata"].pop(k, None)
print(json.dumps(cm))' | kubectl apply -f -
kubectl -n shop rollout restart deploy/api
kubectl -n shop rollout status deploy/api --timeout=120s
