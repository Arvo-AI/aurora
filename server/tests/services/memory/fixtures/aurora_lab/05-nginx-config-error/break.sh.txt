kubectl -n shop get cm frontend-nginx -o json \
  | sed 's/proxy_connect_timeout/proxy_conect_timeout/' \
  | kubectl apply -f -
kubectl -n shop rollout restart deploy/frontend
