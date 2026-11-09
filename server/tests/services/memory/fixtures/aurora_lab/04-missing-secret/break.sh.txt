kubectl -n shop patch deploy api --type=json -p='[{"op":"add","path":"/spec/template/spec/containers/0/env/-","value":{
  "name":"DB_PASSWORD","valueFrom":{"secretKeyRef":{"name":"api-secrets","key":"db-password"}}}}]'
