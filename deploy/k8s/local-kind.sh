#!/usr/bin/env bash
# Local kind E2E: build images, load into kind, apply manifests.
# Requires: Docker, kind, kubectl. Example: kind create cluster --name minicloud
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

docker build -t minicloud/egress-http:latest -f services/egressServices/http/Dockerfile services/egressServices/http
docker build -t minicloud/egress-ftp:latest -f services/egressServices/ftp/Dockerfile services/egressServices/ftp
docker build -t minicloud/egress-ssh:latest -f services/egressServices/ssh/Dockerfile services/egressServices/ssh
docker build -t minicloud/egress-rabbitmq:latest -f services/egressServices/rabbitmq/Dockerfile services/egressServices/rabbitmq
docker build -t minicloud/transformers:latest -f services/transformers/Dockerfile services/transformers
docker build -t minicloud/storage:latest -f services/storage/Dockerfile services/storage
docker build -t minicloud/identity:latest -f services/identity/Dockerfile services/identity
docker build -t minicloud/orchestrator:latest -f services/orchestrator/Dockerfile services/orchestrator
docker build -t minicloud/gateway:latest -f services/gateway/Dockerfile services/gateway

CLUSTER="${KIND_CLUSTER_NAME:-minicloud}"
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  kind load docker-image minicloud/egress-http:latest --name "$CLUSTER"
  kind load docker-image minicloud/egress-ftp:latest --name "$CLUSTER"
  kind load docker-image minicloud/egress-ssh:latest --name "$CLUSTER"
  kind load docker-image minicloud/egress-rabbitmq:latest --name "$CLUSTER"
  kind load docker-image minicloud/transformers:latest --name "$CLUSTER"
  kind load docker-image minicloud/storage:latest --name "$CLUSTER"
  kind load docker-image minicloud/identity:latest --name "$CLUSTER"
  kind load docker-image minicloud/orchestrator:latest --name "$CLUSTER"
  kind load docker-image minicloud/gateway:latest --name "$CLUSTER"
else
  echo "No kind cluster named '$CLUSTER'. Create one: kind create cluster --name $CLUSTER" >&2
  exit 1
fi

kubectl apply -k deploy/k8s
echo "Done. Gateway: kubectl port-forward svc/gateway 8080:8080  (if you are not using Ingress)"
