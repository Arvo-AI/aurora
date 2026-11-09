kubectl -n shop apply -f - <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata:
  name: shop-api
  namespace: shop
data:
  app.py: |
    import json, os, traceback
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def settings():
        return {"db": os.environ["DATABASE_URL"], "redis": os.environ.get("REDIS_HOST", "redis")}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/healthz":
                return self._send(200, {"status": "ok"})
            try:
                settings()
                self._send(200, {"items": []})
            except Exception:
                traceback.print_exc()
                self._send(500, {"error": "internal server error"})

        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            print("%s %s" % (self.address_string(), fmt % args), flush=True)

    print("shop-api 0.9.3 listening on :8080", flush=True)
    ThreadingHTTPServer(("", 8080), H).serve_forever()
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: shop-api
  namespace: shop
spec:
  replicas: 1
  selector:
    matchLabels: {app: api, app.kubernetes.io/name: shop-api}
  template:
    metadata:
      labels: {app: api, app.kubernetes.io/name: shop-api}
    spec:
      containers:
        - name: api
          image: python:3.12-alpine
          command: ["python", "-u", "/code/app.py"]
          env:
            - {name: REDIS_HOST, value: redis}
          ports: [{containerPort: 8080}]
          readinessProbe:
            httpGet: {path: /healthz, port: 8080}
            periodSeconds: 5
          resources:
            requests: {cpu: 50m, memory: 32Mi}
            limits: {memory: 128Mi}
          volumeMounts: [{name: code, mountPath: /code}]
      volumes:
        - name: code
          configMap: {name: shop-api}
YAML
