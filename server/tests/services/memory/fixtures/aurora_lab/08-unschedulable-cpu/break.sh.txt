kubectl -n shop patch deploy worker --type=json -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/requests/cpu","value":"64"}]'
