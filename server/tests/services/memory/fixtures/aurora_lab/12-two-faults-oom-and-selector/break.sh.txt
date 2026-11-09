kubectl -n shop set env deploy/worker BATCH_SIZE=2500
kubectl -n shop patch svc frontend -p '{"spec":{"selector":{"app":"frontend","tier":"web"}}}'
