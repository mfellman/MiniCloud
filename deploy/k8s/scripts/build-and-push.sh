#!/usr/bin/env bash
# MiniCloud: bouw alle Docker-images en push ze naar de GitLab Container Registry.
# Configuratie: deploy-config.local.env (dezelfde als gitlab-deploy.sh).
#
# Gebruik:
#   bash deploy/k8s/scripts/build-and-push.sh          # bouw + push alle services
#   bash deploy/k8s/scripts/build-and-push.sh gateway   # alleen gateway
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_LOCAL="$SCRIPT_DIR/deploy-config.local.env"

if [[ ! -f "$CONFIG_LOCAL" ]]; then
  echo "Geen $CONFIG_LOCAL gevonden — kopieer en vul eerst in:" >&2
  echo "  cp deploy-config.example.env deploy-config.local.env" >&2
  exit 1
fi

set -a
# shellcheck source=/dev/null
source "$CONFIG_LOCAL"
set +a

: "${REGISTRY_PREFIX:?Zet REGISTRY_PREFIX in deploy-config.local.env}"
TAG="${EXPLICIT_TAG:-latest}"

# Repo root: drie niveaus omhoog vanuit scripts/
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

declare -A SERVICES=(
  [gateway]="services/gateway"
  [orchestrator]="services/orchestrator"
  [transformers]="services/transformers"
  [egress-http]="services/egressServices/http"
  [egress-ftp]="services/egressServices/ftp"
  [egress-ssh]="services/egressServices/ssh"
  [dashboard]="services/dashboard"
)

build_push() {
  local name="$1"
  local context="$REPO_ROOT/${SERVICES[$name]}"
  local image="${REGISTRY_PREFIX}/${name}:${TAG}"

  if [[ ! -f "$context/Dockerfile" ]]; then
    echo "FOUT: geen Dockerfile in $context" >&2
    return 1
  fi

  echo "=== Build: $image ==="
  if ! docker build -t "$image" "$context"; then
    echo "FOUT: docker build mislukt voor $name" >&2
    return 1
  fi

  echo "=== Push:  $image ==="
  if ! docker push "$image"; then
    echo "FOUT: docker push mislukt voor $name" >&2
    return 1
  fi

  echo "--- $name OK ---"
  echo
}

# Optioneel: alleen bepaalde services bouwen (argumenten)
TARGETS=("$@")
if [[ ${#TARGETS[@]} -eq 0 ]]; then
  TARGETS=("gateway" "orchestrator" "transformers" "egress-http" "egress-ftp" "egress-ssh" "dashboard")
fi

FAILED=()
for svc in "${TARGETS[@]}"; do
  if [[ -z "${SERVICES[$svc]+x}" ]]; then
    echo "Onbekende service: $svc (kies uit: ${!SERVICES[*]})" >&2
    FAILED+=("$svc")
    continue
  fi
  if ! build_push "$svc"; then
    FAILED+=("$svc")
  fi
done

echo "==============================="
if [[ ${#FAILED[@]} -gt 0 ]]; then
  echo "MISLUKT: ${FAILED[*]}"
  exit 1
else
  echo "Alle images gebouwd en gepusht naar ${REGISTRY_PREFIX} (tag: ${TAG})"
fi
