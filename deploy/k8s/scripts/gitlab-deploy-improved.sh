#!/usr/bin/env bash
# MiniCloud: optioneel repo clonen/updaten, daarna tijdelijke Kustomize-overlay + kubectl apply.
# Configuratie: deploy-config.local.env (zie deploy-config.example.env).
# 
# AUTHENTICATIE-GIDS:
# - Git SSH: zorg dat SSH agent loopt (ssh-agent, Pageant in PuTTY)
# - Git HTTPS: stel GIT_CREDENTIALS_HELPER in of zet environment vars
# - Docker registry: GITLAB_REGISTRY_USER & PASSWORD in deploy-config.local.env
# - Kubectl: KUBECONFIG omgevingsvariabele of ~/.kube/config
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

# AUTHENTICATIE SETUP
# ==================

# Enable verbose output voor debugging
DEBUG_MODE="${DEBUG_MODE:-false}"
if [[ "$DEBUG_MODE" == "true" ]]; then
  set -x
fi

# Git credentials configuratie
# Voor SSH: zorg dat ssh-agent loopt / Pageant actief is
# Voor HTTPS: gebruik GIT_CREDENTIALS_HELPER of Git credentials store
GIT_CREDENTIALS_HELPER="${GIT_CREDENTIALS_HELPER:-}" # bijv. "store" of "cache"
if [[ -n "$GIT_CREDENTIALS_HELPER" ]]; then
  git config --global credential.helper "$GIT_CREDENTIALS_HELPER"
fi

# SSH agent forwarding (voor PuTTY/RemoteDesktop scenario's)
# Zorg dat SSH_AUTH_SOCK juist is ingesteld als je SSH forwarding gebruikt
if [[ "${ENABLE_SSH_AGENT_FORWARD:-false}" == "true" ]]; then
  if [[ -z "${SSH_AUTH_SOCK:-}" ]]; then
    echo "Waarschuwing: ENABLE_SSH_AGENT_FORWARD=true maar SSH_AUTH_SOCK niet ingesteld" >&2
    echo "  In PuTTY: Settings > Connection > SSH > Auth > Allow agent forwarding" >&2
    echo "  Op initiator machine: eval \$(ssh-agent -s) && ssh-add ~/.ssh/id_rsa" >&2
  fi
fi

if [[ ! -f "$CONFIG_LOCAL" ]]; then
  echo "Geen $CONFIG_LOCAL — kopieer en vul eerst in:" >&2
  echo "  cp $CONFIG_EXAMPLE $CONFIG_LOCAL" >&2
  echo "  # Edit config: zet REGISTRY_PREFIX, NAMESPACE, GIT_REPO_URL, etc." >&2
  echo "" >&2
  echo "AUTHENTICATIE SETUP NODIG:" >&2
  echo "  1. Git SSH: zorg dat SSH agent (ssh-agent/Pageant) actief is" >&2
  echo "  2. Git HTTPS: use GIT_REPO_URL met https:// en optioneel GIT_CREDENTIALS_HELPER" >&2
  echo "  3. Docker registry: zet GITLAB_REGISTRY_USER & PASSWORD" >&2
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
: "${GIT_REF:=develop}"
: "${GIT_CLONE_DEPTH:=1}"
: "${CREATE_PULL_SECRET:=false}"
: "${PULL_SECRET_NAME:=gitlab-registry}"
: "${GITLAB_REGISTRY_SERVER:=registry.gitlab.com}"
: "${PATCH_DEPLOYMENTS_PULL_SECRET:=true}"
: "${DRY_RUN:=false}"
: "${PRINT_ONLY:=false}"

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

if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl niet gevonden; installeer kubectl." >&2
  exit 1
fi

# DEBUGGING: check credentials beschikbaarheid
check_credentials() {
  local has_issues=0
  
  echo "Checking credentials..."
  
  # Check SSH agent (voor git SSH)
  if [[ -S "${SSH_AUTH_SOCK:-}" ]]; then
    echo "  ✓ SSH agent actief"
  else
    if [[ -z "${GIT_REPO_URL:-}" ]] || [[ "$GIT_REPO_URL" == ssh://* ]]; then
      echo "  ⚠ SSH agent niet actief - SSH git clone kan falen"
      echo "    Zet up: eval \$(ssh-agent -s) && ssh-add ~/.ssh/id_rsa"
      has_issues=1
    fi
  fi
  
  # Check kubectl access
  if kubectl auth can-i get pods -n "$NAMESPACE" >/dev/null 2>&1 || kubectl auth can-i create namespace >/dev/null 2>&1; then
    echo "  ✓ kubectl heeft voldoende rechten"
  else
    echo "  ⚠ kubectl rechten twijfelachtig - check kubeconfig"
    has_issues=1
  fi
  
  return $has_issues
}

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
    echo "  GIT_REPO_URL=$GIT_REPO_URL"
    
    mkdir -p "$(dirname "$REPO_ROOT")"
    local depth_args=()
    if [[ -n "$GIT_CLONE_DEPTH" && "$GIT_CLONE_DEPTH" != "0" ]]; then
      depth_args=(--depth "$GIT_CLONE_DEPTH")
    fi
    
    # Clone met SSH agent forwarding support
    if GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o StrictHostKeyChecking=no}" \
       git clone "${depth_args[@]}" --branch "$GIT_REF" "$GIT_REPO_URL" "$REPO_ROOT"; then
      echo "✓ Clone gelukt"
    else
      echo "✗ Clone MISLUKT" >&2
      echo "  Controleer:" >&2
      echo "    1. GIT_REPO_URL is correct: $GIT_REPO_URL" >&2
      echo "    2. SSH agent (ssh-agent/Pageant) loopt als je SSH gebruikt" >&2
      echo "    3. Git credentials als je HTTPS gebruikt" >&2
      exit 1
    fi
    return 0
  fi

  if [[ "$UPDATE_GIT_BEFORE_DEPLOY" != "true" ]]; then
    return 0
  fi

  echo "Git bijwerken in $REPO_ROOT (branch $GIT_REF)..."
  if git -C "$REPO_ROOT" fetch origin; then
    git -C "$REPO_ROOT" checkout "$GIT_REF"
    git -C "$REPO_ROOT" pull --ff-only origin "$GIT_REF"
    echo "✓ Git pull gelukt"
  else
    echo "✗ Git fetch/pull MISLUKT" >&2
    echo "  Dit kan voorkomen als:" >&2
    echo "    - Geen internet connectie" >&2
    echo "    - Git credentials ontbreken" >&2
    echo "    - Bepaalde branch bestaat niet" >&2
    exit 1
  fi
}

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
EOF
} >"$KUST"

echo ""
echo "=== DEPLOYMENT CONFIGURATIE ==="
echo "Namespace:      $NAMESPACE"
echo "Tag:            $TAG"
echo "Tag mode:       $TAG_MODE"
echo "Registry:       $REGISTRY_PREFIX"
echo "Repo:           $REPO_ROOT"
echo "Dry-run:        $DRY_RUN"
echo ""

if [[ "$PRINT_ONLY" == "true" ]]; then
  echo "--- gegenereerde kustomization:"
  cat "$KUST"
  exit 0
fi

# CHECK CREDENTIALS
check_credentials || {
  echo ""
  echo "⚠ Potentiële authenticatie-problemen gedetecteerd."
  echo "Type 'yes' om toch door te gaan, of Ctrl+C om af te breken:"
  read -r response
  if [[ "$response" != "yes" ]]; then
    exit 1
  fi
}

echo ""
echo "=== DEPLOYMENT START ==="
echo ""

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

if [[ "$CREATE_PULL_SECRET" == "true" ]]; then
  if [[ -z "${GITLAB_REGISTRY_USER:-}" || -z "${GITLAB_REGISTRY_PASSWORD:-}" ]]; then
    echo "✗ CREATE_PULL_SECRET=true maar GITLAB_REGISTRY_USER/PASSWORD ontbreekt" >&2
    exit 1
  fi
  echo "Creating pull secret: $PULL_SECRET_NAME"
  kubectl create secret docker-registry "$PULL_SECRET_NAME" \
    --namespace="$NAMESPACE" \
    --docker-server="$GITLAB_REGISTRY_SERVER" \
    --docker-username="$GITLAB_REGISTRY_USER" \
    --docker-password="$GITLAB_REGISTRY_PASSWORD" \
    --dry-run=client -o yaml | kubectl apply -f -
  echo "✓ Pull secret bijgewerkt."
fi

APPLY_ARGS=()
if [[ "$DRY_RUN" == "true" ]]; then
  APPLY_ARGS+=(--dry-run=server)
  echo "Dry-run (server): geen permanente wijzigingen."
fi

echo "Applying kustomization..."
kubectl apply "${APPLY_ARGS[@]}" -k "$TMP"

if [[ "$PATCH_DEPLOYMENTS_PULL_SECRET" == "true" && "$DRY_RUN" != "true" ]]; then
  echo "Patching deployments met pull secret..."
  for d in gateway orchestrator transformers egress-http egress-ftp egress-ssh; do
    if kubectl get deployment "$d" -n "$NAMESPACE" >/dev/null 2>&1; then
      kubectl patch deployment "$d" -n "$NAMESPACE" --type merge -p \
        "{\"spec\":{\"template\":{\"spec\":{\"imagePullSecrets\":[{\"name\":\"${PULL_SECRET_NAME}\"}]}}}}"
      echo "  ✓ $d gepatched"
    fi
  done
fi

echo ""
echo "✓ Klaar!"
echo ""
echo "Controleer deployment status:"
echo "  kubectl get pods -n $NAMESPACE"
echo "  kubectl logs -n $NAMESPACE deployment/orchestrator"
echo ""
