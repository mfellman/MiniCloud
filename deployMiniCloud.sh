export NS=minicloud
export REG=registry.gitlab.com
export REG_USER="MarcFellman"
export REG_PASS="XXXX"

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret docker-registry gitlab-registry \
  --namespace "$NS" \
  --docker-server="$REG" \
  --docker-username="$REG_USER" \
  --docker-password="$REG_PASS"
