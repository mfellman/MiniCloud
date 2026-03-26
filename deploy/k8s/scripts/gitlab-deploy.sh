#!/usr/bin/env bash
# MiniCloud: optioneel repo clonen/updaten, daarna tijdelijke Kustomize-overlay + kubectl apply.
# Configuratie: deploy-config.local.env (zie deploy-config.example.env).
#
# Vereist: kubectl, git (voor sync/clone en TAG_MODE=git_short).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_CANDIDATE_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
if [[ -d "$_CANDIDATE_ROOT/deploy/k8s" ]]; then
  DEFAULT_REPO_ROOT="$_CANDIDATE_ROOT"
else
  DEFAULT_REPO_ROOT=""
fi

CONFIG_LOCAL="$SCRIPT_DIR/deploy-config.local.env"
CONFIG_EXAMPLE="$SCRIPT_DIR/deploy-config.example.env"

if [[ ! -f "$CONFIG_LOCAL" ]]; then
  echo "Geen $CONFIG_LOCAL — kopieer en vul eerst in:" >&2
  echo "  cp $CONFIG_EXAMPLE $CONFIG_LOCAL" >&2
  exit 1
fi

# shellcheck source=/dev/null
set -a
source "$CONFIG_LOCAL"
set +a

REPO_ROOT="${REPO_ROOT:-$DEFAULT_REPO_ROOT}"
if [[ -z "$REPO_ROOT" ]]; then
  echo "Zet REPO_ROOT in deploy-config.local.env (pad waar MiniCloud naartoe moet staan)." >&2
  echo "Op een controller zonder repo: bijv. REPO_ROOT=/opt/minicloud en GIT_REPO_URL=..." >&2
  exit 1
fi

: "${REGISTRY_PREFIX:?Zet REGISTRY_PREFIX in deploy-config.local.env}"
: "${NAMESPACE:=minicloud}"
: "${TAG_MODE:=latest}"
: "${GIT_REF:=development}"
: "${GIT_CLONE_DEPTH:=1}"
: "${CREATE_PULL_SECRET:=false}"
: "${PULL_SECRET_NAME:=gitlab-registry}"
: "${GITLAB_REGISTRY_SERVER:=registry.gitlab.com}"
: "${PATCH_DEPLOYMENTS_PULL_SECRET:=true}"
: "${DRY_RUN:=false}"
: "${PRINT_ONLY:=false}"
: "${BUILD_AFTER_SYNC:=true}"

# auto: bij TAG_MODE=latest eerst repo bijwerken (aanbevolen op controller)
UPDATE_GIT_BEFORE_DEPLOY="${UPDATE_GIT_BEFORE_DEPLOY:-auto}"
if [[ "$UPDATE_GIT_BEFORE_DEPLOY" == "auto" ]]; then
  if [[ "$TAG_MODE" == "latest" ]]; then
    UPDATE_GIT_BEFORE_DEPLOY=true
  else
    UPDATE_GIT_BEFORE_DEPLOY=false
  fi
fi

if ! command -v git >/dev/null 2>&1; then
  echo "git niet gevonden; installeer git." >&2
  exit 1
fi

sync_git_repo() {
  if [[ ! -d "$REPO_ROOT/.git" ]]; then
    if [[ -z "${GIT_REPO_URL:-}" ]]; then
      echo "Geen git-repository in $REPO_ROOT (.git ontbreekt)." >&2
      echo "Zet GIT_REPO_URL in deploy-config.local.env om automatisch te clonen," >&2
      echo "of clone handmatig naar REPO_ROOT." >&2
      exit 1
    fi
    if [[ -e "$REPO_ROOT" ]] && [[ -n "$(ls -A "$REPO_ROOT" 2>/dev/null)" ]]; then
      echo "REPO_ROOT bestaat en is niet leeg, maar is geen git-repo: $REPO_ROOT" >&2
      exit 1
    fi
    echo "Clonen MiniCloud-repo naar $REPO_ROOT (branch $GIT_REF, depth $GIT_CLONE_DEPTH)..."
    mkdir -p "$(dirname "$REPO_ROOT")"
    local depth_args=()
    if [[ -n "$GIT_CLONE_DEPTH" && "$GIT_CLONE_DEPTH" != "0" ]]; then
      depth_args=(--depth "$GIT_CLONE_DEPTH")
    fi
    git clone "${depth_args[@]}" --branch "$GIT_REF" "$GIT_REPO_URL" "$REPO_ROOT"
    return 0
  fi

  if [[ "$UPDATE_GIT_BEFORE_DEPLOY" != "true" ]]; then
    return 0
  fi

  # Zorg dat de remote de juiste URL (met credentials) heeft
  if [[ -n "${GIT_REPO_URL:-}" ]]; then
    git -C "$REPO_ROOT" remote set-url origin "$GIT_REPO_URL"
  fi

  echo "Git bijwerken in $REPO_ROOT (branch $GIT_REF)..."
  git -C "$REPO_ROOT" fetch origin
  git -C "$REPO_ROOT" checkout "$GIT_REF"
  git -C "$REPO_ROOT" pull --ff-only origin "$GIT_REF"
}

sync_git_repo

# --- Docker build + push (standaard na elke sync/checkout) ----------------
if [[ "$BUILD_AFTER_SYNC" == "true" ]]; then
  echo "Docker images bouwen en pushen..."
  if ! bash "$SCRIPT_DIR/build-and-push.sh"; then
    echo "FOUT: build-and-push.sh mislukt — deploy wordt afgebroken." >&2
    exit 1
  fi
else
  echo "BUILD_AFTER_SYNC=false — images worden niet opnieuw gebouwd."
fi

resolve_tag() {
  case "$TAG_MODE" in
    latest)
      echo latest
      ;;
    explicit)
      if [[ -z "${EXPLICIT_TAG:-}" ]]; then
        echo "TAG_MODE=explicit vereist EXPLICIT_TAG in deploy-config.local.env" >&2
        exit 1
      fi
      echo "$EXPLICIT_TAG"
      ;;
    git_short)
      git -C "$REPO_ROOT" rev-parse --short HEAD
      ;;
    *)
      echo "Onbekende TAG_MODE: $TAG_MODE (gebruik latest, explicit of git_short)" >&2
      exit 1
      ;;
  esac
}

TAG="$(resolve_tag)"

if [[ ! -d "$REPO_ROOT/deploy/k8s" ]]; then
  echo "REPO_ROOT ziet er niet uit als MiniCloud (mist: $REPO_ROOT/deploy/k8s)" >&2
  exit 1
fi

TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

# Bereken relative path van TMP naar REPO_ROOT/deploy/k8s (Kustomize accepteert geen absolute paden)
RELATIVE_PATH=$(python3 -c "import os; print(os.path.relpath('${REPO_ROOT}/deploy/k8s', '${TMP}'))" 2>/dev/null || echo "${REPO_ROOT}/deploy/k8s")

KUST="$TMP/kustomization.yaml"
{
  cat <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: ${NAMESPACE}
resources:
  - ${RELATIVE_PATH}
images:
  - name: minicloud/gateway
    newName: ${REGISTRY_PREFIX}/gateway
    newTag: ${TAG}
  - name: minicloud/transformers
    newName: ${REGISTRY_PREFIX}/transformers
    newTag: ${TAG}
  - name: minicloud/storage
    newName: ${REGISTRY_PREFIX}/storage
    newTag: ${TAG}
  - name: minicloud/identity
    newName: ${REGISTRY_PREFIX}/identity
    newTag: ${TAG}
  - name: minicloud/orchestrator
    newName: ${REGISTRY_PREFIX}/orchestrator
    newTag: ${TAG}
  - name: minicloud/egress-http
    newName: ${REGISTRY_PREFIX}/egress-http
    newTag: ${TAG}
  - name: minicloud/egress-ftp
    newName: ${REGISTRY_PREFIX}/egress-ftp
    newTag: ${TAG}
  - name: minicloud/egress-ssh
    newName: ${REGISTRY_PREFIX}/egress-ssh
    newTag: ${TAG}
  - name: minicloud/dashboard
    newName: ${REGISTRY_PREFIX}/dashboard
    newTag: ${TAG}
EOF
} >"$KUST"

echo "MiniCloud deploy — namespace=$NAMESPACE tag=$TAG registry=$REGISTRY_PREFIX repo=$REPO_ROOT"
if [[ "$PRINT_ONLY" == "true" ]]; then
  echo "--- gegenereerde kustomization:"
  cat "$KUST"
  exit 0
fi

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

if [[ "$CREATE_PULL_SECRET" == "true" ]]; then
  if [[ -z "${GITLAB_REGISTRY_USER:-}" || -z "${GITLAB_REGISTRY_PASSWORD:-}" ]]; then
    echo "CREATE_PULL_SECRET=true maar GITLAB_REGISTRY_USER/PASSWORD ontbreekt" >&2
    exit 1
  fi
  kubectl create secret docker-registry "$PULL_SECRET_NAME" \
    --namespace="$NAMESPACE" \
    --docker-server="$GITLAB_REGISTRY_SERVER" \
    --docker-username="$GITLAB_REGISTRY_USER" \
    --docker-password="$GITLAB_REGISTRY_PASSWORD" \
    --dry-run=client -o yaml | kubectl apply -f -
  echo "Pull secret $PULL_SECRET_NAME bijgewerkt."
fi

APPLY_ARGS=()
if [[ "$DRY_RUN" == "true" ]]; then
  APPLY_ARGS+=(--dry-run=server)
  echo "Dry-run (server): geen permanente wijzigingen."
fi

kubectl apply "${APPLY_ARGS[@]}" -k "$TMP"

if [[ "$PATCH_DEPLOYMENTS_PULL_SECRET" == "true" && "$DRY_RUN" != "true" ]]; then
  for d in gateway orchestrator transformers storage identity egress-http egress-ftp egress-ssh dashboard; do
    if kubectl get deployment "$d" -n "$NAMESPACE" >/dev/null 2>&1; then
      kubectl patch deployment "$d" -n "$NAMESPACE" --type merge -p \
        "{\"spec\":{\"template\":{\"spec\":{\"imagePullSecrets\":[{\"name\":\"${PULL_SECRET_NAME}\"}]}}}}"
    fi
  done
fi

# Rollout restart zodat pods altijd de nieuwste image ophalen (belangrijk bij tag=latest)
if [[ "$DRY_RUN" != "true" && "$TAG" == "latest" ]]; then
  echo "Rollout restart van alle deployments (tag=latest)..."
  for d in gateway orchestrator transformers storage identity egress-http egress-ftp egress-ssh dashboard; do
    if kubectl get deployment "$d" -n "$NAMESPACE" >/dev/null 2>&1; then
      kubectl rollout restart deployment/"$d" -n "$NAMESPACE"
    fi
  done
  echo "Wacht op rollout..."
  for d in gateway orchestrator transformers storage identity egress-http egress-ftp egress-ssh dashboard; do
    if kubectl get deployment "$d" -n "$NAMESPACE" >/dev/null 2>&1; then
      kubectl rollout status deployment/"$d" -n "$NAMESPACE" --timeout=120s || true
    fi
  done
fi

echo "Klaar. Controleer: kubectl get pods -n $NAMESPACE"
