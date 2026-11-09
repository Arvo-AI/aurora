kubectl -n shop apply -f - <<'YAML'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: redis-data
  namespace: shop
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: fast-ssd
  resources:
    requests: {storage: 1Gi}
YAML
kubectl -n shop patch deploy redis --type=json -p='[
  {"op":"add","path":"/spec/template/spec/volumes","value":[{"name":"data","persistentVolumeClaim":{"claimName":"redis-data"}}]},
  {"op":"add","path":"/spec/template/spec/containers/0/volumeMounts","value":[{"name":"data","mountPath":"/data"}]},
  {"op":"add","path":"/spec/strategy","value":{"type":"Recreate"}}]'
# api pods only connect at startup; restart them as a routine deploy would
kubectl -n shop rollout restart deploy/api
