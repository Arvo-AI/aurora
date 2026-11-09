kubectl -n shop get cm frontend-nginx -o json \
  | sed 's/server api:8080;/server api:8000;/' \
  | kubectl apply -f -
kubectl -n shop delete "$(kubectl -n shop get pods -l app=frontend -o name | head -1)" --wait=false
