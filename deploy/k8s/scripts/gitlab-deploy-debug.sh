#!/usr/bin/env bash
# MiniCloud deployment debugging tool — helpt met authenticatie & credential problemen.
# Voer dit eest uit voordat je gitlab-deploy.sh aanroept.

set -euo pipefail

echo "=== MiniCloud Deployment Debug Tool ==="
echo ""

# 1. Check git credentials
echo "1. GIT CREDENTIALS CHECK"
echo "========================="
if command -v git >/dev/null; then
  echo "✓ git is geïnstalleerd"
  git --version
  echo ""
  
  # Check SSH agent
  if [[ -S "${SSH_AUTH_SOCK:-}" ]]; then
    echo "✓ SSH agent is actief (SSH_AUTH_SOCK=$SSH_AUTH_SOCK)"
    ssh-add -l && echo "  SSH keys in agent:" && ssh-add -l | awk '{print "   -", $NF}'
  else
    echo "✗ SSH agent niet actief. Zet up voor SSH keys:"
    echo "  Linux/Mac: eval \$(ssh-agent -s) && ssh-add ~/.ssh/id_rsa"
    echo "  Windows PuTTY: zorg dat Pageant actief is met je SSH key"
  fi
  echo ""
  
  # Check git config
  echo "Git configuratie:"
  git config --global user.name && echo "  user.name: $(git config --global user.name)" || echo "  ✗ user.name niet ingesteld"
  git config --global user.email && echo "  user.email: $(git config --global user.email)" || echo "  ✗ user.email niet ingesteld"
  echo ""
else
  echo "✗ git is niet geïnstalleerd!"
  exit 1
fi

# 2. Check kubectl
echo "2. KUBECTL & KUBERNETES CHECK"
echo "=============================="
if command -v kubectl >/dev/null; then
  echo "✓ kubectl is geïnstalleerd"
  kubectl version --client 2>/dev/null | head -1
  echo ""
  
  # Check kubeconfig
  if [[ -f "${KUBECONFIG:-$HOME/.kube/config}" ]]; then
    echo "✓ kubeconfig gevonden: ${KUBECONFIG:-$HOME/.kube/config}"
    echo "  Beschikbare clusters:"
    kubectl config get-clusters | sed 's/^/    /'
    echo ""
    CURRENT=$(kubectl config current-context 2>/dev/null || echo "(geen)")
    echo "  Current context: $CURRENT"
    echo ""
  else
    echo "✗ kubeconfig niet gevonden! Zet KUBECONFIG of plaats ~/.kube/config"
    exit 1
  fi
else
  echo "✗ kubectl is niet geïnstalleerd!"
  exit 1
fi

# 3. Check Docker registry credentials
echo "3. DOCKER REGISTRY CREDENTIALS CHECK"
echo "====================================="
if [[ -f "$HOME/.docker/config.json" ]]; then
  echo "✓ Docker config gevonden"
  echo "  Ingelogde registries:"
  grep -o '"auths"' "$HOME/.docker/config.json" >/dev/null && \
    jq -r '.auths | keys[]' "$HOME/.docker/config.json" 2>/dev/null | sed 's/^/    /' || \
    echo "    (geen)"
else
  echo "⚠ ~/.docker/config.json niet gevonden"
  echo "  Voor lokaal debuggen: docker login <registry>"
  echo "  Voor Kubernetes: moet via pull secret (zie deploy-config.local.env)"
fi
echo ""

# 4. Check deploy config
echo "4. DEPLOYMENT CONFIG CHECK"
echo "=========================="
CONFIG_LOCAL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy-config.local.env"
CONFIG_EXAMPLE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy-config.example.env"

if [[ -f "$CONFIG_LOCAL" ]]; then
  echo "✓ deploy-config.local.env gevonden"
  echo ""
  echo "  Configuratie:"
  # Lees config en print waarden (zonder wachtwoorden)
  while IFS='=' read -r key value; do
    if [[ "$key" =~ ^[^#] ]] && [[ -n "$key" ]]; then
      if [[ "$key" == *PASSWORD* ]] || [[ "$key" == *TOKEN* ]] || [[ "$key" == *SECRET* ]]; then
        echo "    $key=(***REDACTED***)"
      elif [[ -n "$value" ]]; then
        echo "    $key=$value"
      fi
    fi
  done < "$CONFIG_LOCAL"
  echo ""
else
  echo "✗ deploy-config.local.env niet gevonden!"
  echo "  Maak hem aan:"
  echo "  cp $CONFIG_EXAMPLE $CONFIG_LOCAL"
  echo "  # Edit $CONFIG_LOCAL met jouw instellingen"
  exit 1
fi

# 5. Network test (kan je bereiken wat je nodig hebt)
echo "5. NETWORK & CONNECTIVITY TEST"
echo "==============================="
# Parse config voor registry en JWKS URL (als die er zijn)
source "$CONFIG_LOCAL" 2>/dev/null || true

if [[ -n "${OAUTH2_JWKS_URI:-}" ]]; then
  JWKS_HOST=$(echo "$OAUTH2_JWKS_URI" | sed 's|https://||; s|/.*||')
  echo "  OAUTH2_JWKS_URI host: $JWKS_HOST"
  if ping -c 1 -W 2 "$JWKS_HOST" >/dev/null 2>&1; then
    echo "  ✓ Bereikbaar"
  else
    echo "  ⚠ Mogelijk onbereikbaar (ping faalde)"
  fi
fi

echo ""
echo "=== DEBUG KLAAR ==="
echo ""
echo "Volgende stappen:"
echo "1. Check alle ✓ en ✗ hierboven"
echo "2. Voor SSH key problemen: zorg dat SSH agent actief is"
echo "3. Voor Docker registry: set GITLAB_REGISTRY_USER & PASSWORD in deploy-config.local.env"
echo "4. Voor kubectl: zorg dat kubeconfig correct is en je de juiste cluster hebt"
echo ""
echo "Voer dan uit: bash gitlab-deploy.sh"
