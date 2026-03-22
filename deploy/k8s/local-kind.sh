#!/usr/bin/env bash
# Lokale E2E op kind: bouwt images, laadt ze in de cluster, past manifests toe.
# Vereiste: Docker, kind, kubectl. Voorbeeld: kind create cluster --name minicloud
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

docker build -t minicloud/xslt:latest -f services/xslt/Dockerfile services/xslt
docker build -t minicloud/httpcall:latest -f services/httpcall/Dockerfile services/httpcall
docker build -t minicloud/orchestrator:latest -f services/orchestrator/Dockerfile services/orchestrator
docker build -t minicloud/gateway:latest -f services/gateway/Dockerfile services/gateway

CLUSTER="${KIND_CLUSTER_NAME:-minicloud}"
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  kind load docker-image minicloud/xslt:latest --name "$CLUSTER"
  kind load docker-image minicloud/httpcall:latest --name "$CLUSTER"
  kind load docker-image minicloud/orchestrator:latest --name "$CLUSTER"
  kind load docker-image minicloud/gateway:latest --name "$CLUSTER"
else
  echo "Geen kind-cluster '$CLUSTER'. Maak er een: kind create cluster --name $CLUSTER" >&2
  exit 1
fi

kubectl apply -k deploy/k8s
echo "Klaar. Gateway: kubectl port-forward svc/gateway 8080:8080  (als je geen Ingress gebruikt)"
