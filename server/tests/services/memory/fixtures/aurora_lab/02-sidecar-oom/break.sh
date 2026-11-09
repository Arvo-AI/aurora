kubectl -n shop patch deploy api --type=json -p='[{"op":"add","path":"/spec/template/spec/containers/-","value":{
  "name":"log-shipper","image":"python:3.12-alpine",
  "command":["python","-u","-c","import time\nprint(\"log-shipper: buffering 40 MB before first flush\", flush=True)\nbuf=bytearray(40*1024*1024)\nwhile True: time.sleep(5)"],
  "resources":{"requests":{"cpu":"10m","memory":"8Mi"},"limits":{"memory":"16Mi"}}}}]'
