kubectl -n shop get cm api-code -o json | python3 -c '
import json, sys
cm = json.load(sys.stdin)
cm["data"]["worker.py"] = """import os, time
batch = int(os.environ.get("BATCH_SIZE", "500"))
print(f"worker started, batch size {batch}", flush=True)
n = 0
while True:
    # Each queued item holds about 100 KiB while the batch is processed.
    buf = [bytearray(100 * 1024) for _ in range(batch)]
    print("WARNING py.warnings: DeprecationWarning: StrictRedis is deprecated and will be removed in redis-py 6.0, use Redis", flush=True)
    print(f"processed batch of {len(buf)} items", flush=True)
    n += 1
    if n % 3 == 0:
        print("ERROR metrics: push to pushgateway:9091 failed: [Errno -2] Name does not resolve (will retry next batch)", flush=True)
    del buf
    time.sleep(5)
"""
for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields"):
    cm["metadata"].pop(k, None)
print(json.dumps(cm))' | kubectl apply -f -
kubectl -n shop rollout restart deploy/worker
kubectl -n shop rollout status deploy/worker --timeout=120s
kubectl -n shop patch svc api --type=json -p='[{"op":"replace","path":"/spec/ports/0/targetPort","value":9090}]'
