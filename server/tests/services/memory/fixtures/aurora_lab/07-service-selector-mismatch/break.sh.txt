kubectl -n shop patch svc api -p '{"spec":{"selector":{"app":"api-v2"}}}'
