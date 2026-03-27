# Workflow and Connection Deployment

This guide describes how to deploy, update, and delete workflow and connection definitions in MiniCloud — without redeploying the cluster or restarting pods.

## How it works

Workflows and connections are stored in the **storage service** (under `/app/data/runtime/`). The orchestrator reads them via `ORCH_RUNTIME_STORE=http` and reloads on demand with a lightweight API call.

```
storage pod  ←  PUT/DELETE /internal/upload/workflows/{name}
                              ↕
orchestrator pod  ←  POST /admin/reload   (hot-reload, < 1 s)
```

No cluster apply, no pod restart, no downtime.

---

## 1. Prerequisites

| What | Where |
|------|-------|
| `STORAGE_SERVICE_ADMIN_TOKEN` | storage pod environment variable (set per env/overlay) |
| `ORCH_RELOAD_TOKEN` | orchestrator pod environment variable |
| `ORCH_RUNTIME_STORE=http` | orchestrator pod environment variable (set in overlays) |
| `STORAGE_SERVICE_URL` | orchestrator pod environment variable (`http://storage:8080`) |

From outside the cluster, route through the gateway or `kubectl port-forward`:

```bash
kubectl port-forward -n minicloud-dev svc/storage 9090:8080 &
kubectl port-forward -n minicloud-dev svc/orchestrator 9091:8080 &

STORAGE_URL=http://localhost:9090
ORCH_URL=http://localhost:9091
```

---

## 2. Upload or update a workflow

Send the raw YAML body to the storage service. A reload call activates it immediately.

```bash
# Variables
STORAGE_URL=http://localhost:9090
ADMIN_TOKEN=<STORAGE_SERVICE_ADMIN_TOKEN>
ORCH_URL=http://localhost:9091
RELOAD_TOKEN=<ORCH_RELOAD_TOKEN>
WORKFLOW_NAME=my-flow          # Must be a-z, 0-9, -, _, .

# 1. Upload (create or replace)
curl -sf -X POST "${STORAGE_URL}/internal/upload/workflows/${WORKFLOW_NAME}" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: text/yaml" \
  --data-binary @workflows/${WORKFLOW_NAME}.yaml

# 2. Trigger live reload on all orchestrator replicas
curl -sf -X POST "${ORCH_URL}/admin/reload" \
  -H "X-Reload-Token: ${RELOAD_TOKEN}"
```

The reload response confirms active counts:

```json
{"status": "reloaded", "workflows": 4, "connections": 2}
```

---

## 3. Delete a workflow

```bash
# 1. Delete from storage
curl -sf -X DELETE "${STORAGE_URL}/internal/workflows/${WORKFLOW_NAME}" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}"

# 2. Reload (removes it from active memory)
curl -sf -X POST "${ORCH_URL}/admin/reload" \
  -H "X-Reload-Token: ${RELOAD_TOKEN}"
```

---

## 4. Upload or update a connection

Same pattern:

```bash
CONNECTION_NAME=my-api

curl -sf -X POST "${STORAGE_URL}/internal/upload/connections/${CONNECTION_NAME}" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: text/yaml" \
  --data-binary @connections/${CONNECTION_NAME}.yaml

curl -sf -X POST "${ORCH_URL}/admin/reload" \
  -H "X-Reload-Token: ${RELOAD_TOKEN}"
```

---

## 5. Inspect what is active

```bash
# List all workflows
curl -s "${STORAGE_URL}/internal/workflows" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" | python -m json.tool

# Get one workflow
curl -s "${STORAGE_URL}/internal/workflows/${WORKFLOW_NAME}" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" | python -m json.tool
```

---

## 6. CI/CD — GitLab CI example

Add a `deploy-workflows` job to `.gitlab-ci.yml`. It runs after the image build
stage and requires no cluster redeploy.

```yaml
# .gitlab-ci.yml (fragment)

variables:
  STORAGE_URL: "https://minicloud-dev.example.com/storage"   # or port-forward
  ORCH_URL:    "https://minicloud-dev.example.com"
  # STORAGE_ADMIN_TOKEN and ORCH_RELOAD_TOKEN are CI/CD variables (masked)

stages:
  - build
  - deploy-images
  - deploy-workflows     # <-- lightweight, no cluster apply

deploy-workflows:dev:
  stage: deploy-workflows
  image: curlimages/curl:latest
  environment: dev
  rules:
    - if: $CI_COMMIT_BRANCH == "develop"
  script:
    - |
      echo "Uploading workflows to DEV..."
      for f in workflows/*.yaml; do
        name="$(basename "$f" .yaml)"
        echo "  → $name"
        curl -sf -X POST "${STORAGE_URL}/internal/upload/workflows/${name}" \
          -H "Authorization: Bearer ${STORAGE_ADMIN_TOKEN}" \
          -H "Content-Type: text/yaml" \
          --data-binary @"${f}"
      done
    - |
      echo "Uploading connections to DEV..."
      for f in connections/*.yaml; do
        name="$(basename "$f" .yaml)"
        echo "  → $name"
        curl -sf -X POST "${STORAGE_URL}/internal/upload/connections/${name}" \
          -H "Authorization: Bearer ${STORAGE_ADMIN_TOKEN}" \
          -H "Content-Type: text/yaml" \
          --data-binary @"${f}"
      done
    - |
      echo "Reloading orchestrator..."
      curl -sf -X POST "${ORCH_URL}/admin/reload" \
        -H "X-Reload-Token: ${ORCH_RELOAD_TOKEN}"

deploy-workflows:prd:
  stage: deploy-workflows
  image: curlimages/curl:latest
  environment: prd
  rules:
    - if: $CI_COMMIT_BRANCH == "main"
      when: manual           # require explicit approval for PRD
  script:
    - |
      for f in workflows/*.yaml; do
        name="$(basename "$f" .yaml)"
        curl -sf -X POST "${STORAGE_URL_PRD}/internal/upload/workflows/${name}" \
          -H "Authorization: Bearer ${STORAGE_ADMIN_TOKEN_PRD}" \
          -H "Content-Type: text/yaml" \
          --data-binary @"${f}"
      done
    - curl -sf -X POST "${ORCH_URL_PRD}/admin/reload" \
        -H "X-Reload-Token: ${ORCH_RELOAD_TOKEN_PRD}"
```

Required CI/CD variables (masked, per environment):

| Variable | Where |
|----------|-------|
| `STORAGE_ADMIN_TOKEN` | storage `STORAGE_SERVICE_ADMIN_TOKEN` |
| `ORCH_RELOAD_TOKEN` | orchestrator `ORCH_RELOAD_TOKEN` |
| `STORAGE_URL` | base URL to reach the storage service |
| `ORCH_URL` | base URL to reach the orchestrator |

---

## 7. Delete a workflow in CI

To remove a workflow that was renamed or retired, add a delete job or a manual cleanup step:

```bash
# In a CI script or manual step:
curl -sf -X DELETE "${STORAGE_URL}/internal/workflows/old-flow-name" \
  -H "Authorization: Bearer ${STORAGE_ADMIN_TOKEN}"

curl -sf -X POST "${ORCH_URL}/admin/reload" \
  -H "X-Reload-Token: ${ORCH_RELOAD_TOKEN}"
```

For systematic cleanup you can diff the local directory against the storage list:

```bash
# Files that no longer exist locally but are still in storage → delete them
local_names=$(ls workflows/*.yaml | xargs -I{} basename {} .yaml | sort)
storage_names=$(curl -s "${STORAGE_URL}/internal/workflows" \
  -H "Authorization: Bearer ${STORAGE_ADMIN_TOKEN}" \
  | python -c "import sys,json; [print(w['name']) for w in json.load(sys.stdin)['workflows']]" \
  | sort)

for name in $(comm -13 <(echo "$local_names") <(echo "$storage_names")); do
  echo "Removing retired workflow: $name"
  curl -sf -X DELETE "${STORAGE_URL}/internal/workflows/${name}" \
    -H "Authorization: Bearer ${STORAGE_ADMIN_TOKEN}"
done

curl -sf -X POST "${ORCH_URL}/admin/reload" \
  -H "X-Reload-Token: ${ORCH_RELOAD_TOKEN}"
```

---

## 8. Promotion: DEV → TST → ACC → PRD

Workflows live in `workflows/`. Promotion follows git revisions:

```
git branch develop  →  CI builds :dev   →  deploy-workflows:dev   (auto)
git branch main     →  CI builds :latest →  deploy-workflows:prd  (manual gate)
```

Because workflows are plain YAML in the repo, branching strategy and code review (MR) are the natural promotion gate — no separate artifact store needed.

---

## 9. Rollback

Rollback is a re-upload of the previous version:

```bash
# From git tag or branch:
git show v1.2.3:workflows/my-flow.yaml > /tmp/my-flow-rollback.yaml

curl -sf -X POST "${STORAGE_URL}/internal/upload/workflows/my-flow" \
  -H "Authorization: Bearer ${STORAGE_ADMIN_TOKEN}" \
  -H "Content-Type: text/yaml" \
  --data-binary @/tmp/my-flow-rollback.yaml

curl -sf -X POST "${ORCH_URL}/admin/reload" \
  -H "X-Reload-Token: ${ORCH_RELOAD_TOKEN}"
```

---

## 10. Multi-replica reload

With multiple orchestrator replicas (e.g. PRD uses 2), `/admin/reload` only hits the instance that handles the request. Use one of these approaches:

**Option A — reload all pods sequentially (recommended for small scale):**

```bash
for pod in $(kubectl get pods -n minicloud-prd -l app.kubernetes.io/name=orchestrator -o name); do
  kubectl exec -n minicloud-prd "$pod" -- \
    curl -sf -X POST http://localhost:8080/admin/reload \
      -H "X-Reload-Token: ${ORCH_RELOAD_TOKEN}"
done
```

**Option B — rolling restart (zero-downtime, uses Kubernetes RollingUpdate):**

```bash
kubectl rollout restart deployment/orchestrator -n minicloud-prd
```

This triggers pod replacement which re-reads from storage on startup. Same end result within ~15 s.
