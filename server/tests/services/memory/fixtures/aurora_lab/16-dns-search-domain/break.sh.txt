dns_ip="$(kubectl -n kube-system get svc kube-dns -o jsonpath='{.spec.clusterIP}')"
kubectl -n shop patch deploy api --type=merge -p "{\"spec\":{\"template\":{\"spec\":{\"dnsPolicy\":\"None\",\"dnsConfig\":{
  \"nameservers\":[\"$dns_ip\"],
  \"searches\":[\"shop.svc.cluster.internal\",\"svc.cluster.internal\",\"cluster.internal\"],
  \"options\":[{\"name\":\"ndots\",\"value\":\"5\"}]}}}}}"
