#!/usr/bin/env bash
set -euo pipefail

PROFILE="minicloud"
NAMESPACE="minicloud"
OVERLAY="deploy/overlays/minikube"
CPUS=4
MEMORY=4096
SKIP_BUILD=false
SKIP_PORT_FORWARD=false
RUN_TESTS=false

usage() {
  cat <<'EOF'
Gebruik:
  ./deploy-minikube.sh [opties]

Opties:
  --skip-build         Sla docker build over.
  --skip-port-forward  Zet geen port-forwards op.
  --run-tests          Draai pytest na deploy.
  --cpus N             Aantal CPU's voor minikube (default: 4).
  --memory MB          Geheugen in MB voor minikube (default: 4096).
  -h, --help           Toon hulp.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-build) SKIP_BUILD=true; shift ;;
    --skip-port-forward) SKIP_PORT_FORWARD=true; shift ;;
    --run-tests) RUN_TESTS=true; shift ;;
    --cpus) CPUS="${2:?missing value for --cpus}"; shift 2 ;;
    --memory) MEMORY="${2:?missing value for --memory}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Onbekende optie: $1" >&2; usage; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
echo "==> Repo root: $SCRIPT_DIR"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Command niet gevonden: $1" >&2; exit 1; }
}

echo "==> Vereiste tools controleren"
require_cmd minikube
require_cmd kubectl
require_cmd docker
echo "    OK minikube, kubectl en docker zijn beschikbaar."

echo "==> Minikube cluster '$PROFILE' controleren / starten"
status="$(minikube status --profile "$PROFILE" --format '{{.Host}}' 2>/dev/null || true)"
if [[ "$status" == "Running" ]]; then
  echo "    OK Cluster draait al."
else
  echo "    Cluster starten (cpus=$CPUS, memory=${MEMORY}MB)..."
  minikube start \
    --profile "$PROFILE" \
    --cpus "$CPUS" \
    --memory "$MEMORY" \
    --driver docker \
    --kubernetes-version stable
  echo "    OK Cluster gestart."
fi

minikube update-context --profile "$PROFILE" >/dev/null
MINIKUBE_IP="$(minikube ip --profile "$PROFILE" 2>/dev/null || true)"

echo "==> Ingress-addon inschakelen"
if minikube addons list --profile "$PROFILE" --output json 2>/dev/null | rg -q '"ingress".*"enabled"'; then
  echo "    OK Ingress was al ingeschakeld."
else
  minikube addons enable ingress --profile "$PROFILE" >/dev/null
  echo "    OK Ingress ingeschakeld."
fi

echo "==> Docker-omgeving instellen voor minikube-daemon"
eval "$(minikube -p "$PROFILE" docker-env)"
echo "    OK DOCKER_HOST=${DOCKER_HOST:-<unset>}"

if [[ "$SKIP_BUILD" == "true" ]]; then
  echo "==> WARN Build overgeslagen (--skip-build)."
else
  echo "==> Docker-images bouwen in minikube-daemon"
  builds=(
    "minicloud/egress-http:latest|services/egressServices/http|services/egressServices/http/Dockerfile"
    "minicloud/egress-ftp:latest|services/egressServices/ftp|services/egressServices/ftp/Dockerfile"
    "minicloud/egress-ssh:latest|services/egressServices/ssh|services/egressServices/ssh/Dockerfile"
    "minicloud/egress-rabbitmq:latest|services/egressServices/rabbitmq|services/egressServices/rabbitmq/Dockerfile"
    "minicloud/transformers:latest|services/transformers|services/transformers/Dockerfile"
    "minicloud/storage:latest|services/storage|services/storage/Dockerfile"
    "minicloud/identity:latest|services/identity|services/identity/Dockerfile"
    "minicloud/orchestrator:latest|services/orchestrator|services/orchestrator/Dockerfile"
    "minicloud/gateway:latest|services/gateway|services/gateway/Dockerfile"
    "minicloud/dashboard:latest|services/dashboard|services/dashboard/Dockerfile"
    "minicloud/scheduler:latest|services/scheduler|services/scheduler/Dockerfile"
  )
  for b in "${builds[@]}"; do
    IFS='|' read -r tag context dockerfile <<< "$b"
    echo "    Bouwen: $tag"
    docker build -t "$tag" -f "$dockerfile" "$context"
  done
  echo "    OK Alle images gebouwd."
fi

echo "==> Namespace '$NAMESPACE' aanmaken (indien nodig)"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo "    OK Namespace klaar."

echo "==> Manifesten toepassen via kustomize ($OVERLAY)"
kubectl apply -k "$OVERLAY"
echo "    OK Manifesten toegepast."

echo "==> Wachten tot alle deployments gereed zijn (max 5 minuten)"
deployments=(
  gateway orchestrator storage identity transformers
  egress-http egress-ftp egress-ssh egress-rabbitmq
  dashboard rabbitmq scheduler
)
for dep in "${deployments[@]}"; do
  echo "    Wachten op: $dep"
  kubectl rollout status "deployment/$dep" -n "$NAMESPACE" --timeout=300s
done
echo "    OK Alle deployments zijn gereed."

PF_PIDS=()
if [[ "$SKIP_PORT_FORWARD" == "true" ]]; then
  echo "==> WARN Port-forwards overgeslagen (--skip-port-forward)."
else
  echo "==> Port-forwards instellen"
  forwards=(
    "8080|gateway|8080"
    "8083|orchestrator|8080"
    "8086|storage|8080"
    "8088|identity|8080"
    "8081|transformers|8080"
    "8082|egress-http|8080"
    "8084|egress-ftp|8080"
    "8085|egress-ssh|8080"
    "8087|egress-rabbitmq|8080"
    "8090|dashboard|8080"
    "8089|scheduler|8089"
  )
  for f in "${forwards[@]}"; do
    IFS='|' read -r local svc remote <<< "$f"
    echo "    localhost:${local} -> svc/${svc}:${remote}"
    kubectl port-forward -n "$NAMESPACE" "svc/$svc" "${local}:${remote}" >/tmp/minicloud-pf-"$svc".log 2>&1 &
    PF_PIDS+=("$!")
  done
  sleep 5
  echo "    OK ${#PF_PIDS[@]} port-forwards actief."
fi

if [[ "$SKIP_PORT_FORWARD" != "true" ]]; then
  echo "==> Workflows & Connections in Storage laden"
  STORAGE_URL="http://localhost:8086"
  ORCH_URL="http://localhost:8083"
  if [[ -d workflows ]]; then
    while IFS= read -r -d '' f; do
      name="$(basename "$f" .yaml)"
      echo "    Upload workflow: $name"
      curl -fsS -X POST "$STORAGE_URL/internal/upload/workflows/$name" \
        -H "Content-Type: application/yaml" \
        --data-binary @"$f" >/dev/null || echo "    WARN upload workflow $name mislukt."
    done < <(find workflows -maxdepth 1 -type f -name '*.yaml' -print0)
  fi
  if [[ -d connections ]]; then
    while IFS= read -r -d '' f; do
      name="$(basename "$f" .yaml)"
      echo "    Upload connection: $name"
      curl -fsS -X POST "$STORAGE_URL/internal/upload/connections/$name" \
        -H "Content-Type: application/yaml" \
        --data-binary @"$f" >/dev/null || echo "    WARN upload connection $name mislukt."
    done < <(find connections -maxdepth 1 -type f -name '*.yaml' -print0)
  fi
  echo "    Orchestrator reload triggeren..."
  curl -fsS -X POST "$ORCH_URL/admin/reload" >/dev/null || echo "    WARN reload mislukt."
fi

if [[ "$RUN_TESTS" == "true" ]]; then
  echo "==> Pytest uitvoeren"
  # Terug naar lokale docker context voor evt. test tooling
  eval "$(minikube -p "$PROFILE" docker-env -u)"
  if [[ -x .venv/bin/python ]]; then
    .venv/bin/python -m pytest -v tests/
  else
    python3 -m pytest -v tests/
  fi
fi

echo
echo "============================================================"
echo " MiniCloud draait in minikube (profiel: $PROFILE)"
echo "============================================================"
echo " Minikube IP: ${MINIKUBE_IP:-unknown}"
echo " Gateway:     http://localhost:8080"
echo " Dashboard:   http://localhost:8090"
echo " Stop pf's:   kill ${PF_PIDS[*]:-<geen>}"
echo " Cleanup:     minikube delete --profile $PROFILE"
