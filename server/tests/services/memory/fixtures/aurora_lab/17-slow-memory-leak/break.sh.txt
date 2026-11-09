kubectl -n shop get cm api-code -o json | python3 -c '
import json, sys
cm = json.load(sys.stdin)
cm["data"]["worker.py"] = """import os, time
batch = int(os.environ.get("BATCH_SIZE", "500"))
print(f"worker started, batch size {batch}", flush=True)
recent = []  # tail of recent batches, so a retried batch can skip items already done
while True:
    # Each queued item holds about 100 KiB while the batch is processed.
    buf = [bytearray(100 * 1024) for _ in range(batch)]
    recent.extend(buf[-50:])
    print(f"processed batch of {len(buf)} items", flush=True)
    del buf
    time.sleep(5)
"""
for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields"):
    cm["metadata"].pop(k, None)
print(json.dumps(cm))' | kubectl apply -f -
kubectl -n shop rollout restart deploy/worker
