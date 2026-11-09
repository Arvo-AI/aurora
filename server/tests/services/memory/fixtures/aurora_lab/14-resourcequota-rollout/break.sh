kubectl -n shop apply -f - <<'YAML'
apiVersion: v1
kind: ResourceQuota
metadata:
  name: compute-resources
  namespace: shop
spec:
  hard:
    requests.memory: 200Mi
YAML
# wait for the quota controller to record usage before the restart
for _ in $(seq 1 30); do
  [ -n "$(kubectl -n shop get resourcequota compute-resources -o jsonpath='{.status.used.requests\.memory}')" ] && break
  sleep 1
done
kubectl -n shop rollout restart deploy/api
