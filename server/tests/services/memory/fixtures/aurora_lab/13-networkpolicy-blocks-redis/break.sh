kubectl -n shop apply -f - <<'YAML'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: redis
  namespace: shop
spec:
  podSelector:
    matchLabels: {app: redis}
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector:
            matchLabels: {app: worker}
      ports:
        - {protocol: TCP, port: 6379}
YAML
