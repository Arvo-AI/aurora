kubectl -n shop patch deploy api --type=json -p='[{"op":"add","path":"/spec/template/spec/initContainers","value":[{
  "name":"wait-for-db","image":"busybox:1.37",
  "command":["sh","-c","until nc -z -w 2 postgres 5432; do echo waiting for postgres:5432; sleep 2; done"],
  "resources":{"requests":{"cpu":"10m","memory":"8Mi"},"limits":{"memory":"16Mi"}}}]}]'
