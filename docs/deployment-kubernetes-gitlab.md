# Deploy MiniCloud on Kubernetes with GitLab Container Registry

This guide describes how to run the **entire** MiniCloud stack on a Kubernetes cluster using images built and stored in the **GitLab Container Registry** (typically via the included [`.gitlab-ci.yml`](../.gitlab-ci.yml)).

For cluster concepts (ConfigMaps, Ingress, `local-kind.sh`), see [Kubernetes (in depth)](kubernetes.md). For GitLab project setup and CI overview, see [GitLab (repo + CI + registry)](gitlab.md). For deploying and updating workflows and connections without cluster apply, see [Workflow deployment](workflow-deployment.md).

---

## 1. What you need

- A **Kubernetes cluster** (managed cloud or self-hosted) with `kubectl` configured.
- A **GitLab project** with:
  - **Container Registry** enabled (**Settings → General → Visibility**).
  - A successful **CI pipeline** that pushed images (see [`.gitlab-ci.yml`](../.gitlab-ci.yml)).
- Permission to create a **namespace**, **Secrets**, and **Deployments** in that cluster.

Images produced by CI (image name = Kaniko `IMAGE_NAME` / path segment):

| Component      | Image name (suffix under `$CI_REGISTRY_IMAGE`) |
|----------------|-----------------------------------------------|
| gateway        | `gateway`                                     |
| transformers   | `transformers`                                |
| orchestrator   | `orchestrator`                                |
| dashboard      | `dashboard`                                   |
| egress-http    | `egress-http`                                 |
| egress-ftp     | `egress-ftp`                                  |
| egress-ssh     | `egress-ssh`                                  |

Full reference for a project:

`registry.gitlab.com/<group>/<project>/<image>:<tag>`

Example: `registry.gitlab.com/acme/minicloud/gateway:latest`

Use a **immutable tag** (commit SHA) for production instead of `:latest` when possible.

---

## 2. Build and publish images

1. Push code to a branch; GitLab CI **build** stage runs Kaniko jobs in parallel.
2. In **Deploy → Container Registry**, confirm images such as `gateway`, `orchestrator`, `dashboard`, `transformers`, etc.
3. Note your registry host (usually `registry.gitlab.com`) and the **project path** (group + project slug).

---

## 3. Cluster pull authentication

The cluster must **pull** private images. Common options:

### Option A — Deploy token (recommended for CI/deploy bots)

1. GitLab: **Settings → Repository → Deploy tokens** — create a token with **`read_registry`** (and `write_registry` only if you also push from cluster).
2. Create a **docker-registry** secret in the target namespace:

```bash
export NS=minicloud
export REG=registry.gitlab.com
export REG_USER="<deploy-token-username>"
export REG_PASS="<deploy-token-password>"

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret docker-registry gitlab-registry \
  --namespace "$NS" \
  --docker-server="$REG" \
  --docker-username="$REG_USER" \
  --docker-password="$REG_PASS"
```

### Option B — Personal access token (PAT)

Use a PAT with **`read_registry`** as the password and your GitLab username (or token username if using a project access token). Prefer deploy tokens or **project/group access tokens** for non-human automation.

### Option C — Public project

If the project and registry are **public**, you may omit `imagePullSecrets` (not recommended for production).

---

## 4. Point manifests at GitLab images

The base manifests under [`deploy/k8s/`](../deploy/k8s/) use placeholder names `minicloud/<service>:latest`. Replace them with your GitLab paths using **Kustomize**.

### Example overlay (included in the repo)

Edit [`deploy/k8s/overlays/gitlab/kustomization.yaml`](../deploy/k8s/overlays/gitlab/kustomization.yaml):

1. Set `namespace` (default `minicloud`) or remove it to use `default`.
2. Replace every `registry.gitlab.com/example-group/example-project` with  
   `registry.gitlab.com/<your-group>/<your-project>` (same prefix CI uses: your **`CI_REGISTRY_IMAGE`** without a trailing image name).

Then build and apply:

```bash
kubectl apply -k deploy/k8s/overlays/gitlab
```

### Attach the pull secret to all deployments

The overlay does **not** patch `imagePullSecrets` (to avoid merge conflicts with future manifest edits). After apply, patch each **Deployment** once:

```bash
NS=minicloud
for d in gateway orchestrator dashboard transformers egress-http egress-ftp egress-ssh; do
  kubectl patch deployment "$d" -n "$NS" --type merge -p \
    '{"spec":{"template":{"spec":{"imagePullSecrets":[{"name":"gitlab-registry"}]}}}}'
done
```

Alternatively, integrate a **Kustomize strategic merge** or **JSON6902 patch** in your own overlay if you prefer GitOps-only workflows.

### Automated rollout script (versioned in repo)

Het script [`deploy/k8s/scripts/gitlab-deploy.sh`](../deploy/k8s/scripts/gitlab-deploy.sh) kan eerst de **git-checkout bijwerken** (of **clonen** als er nog geen repo op de machine staat), genereert daarna een **tijdelijke Kustomize-overlay** (zelfde `images:` als `overlays/gitlab`) en past toe met `kubectl apply -k`.

**Repository op de controller**

- Staat het script **in een bestaande clone** en is `deploy/k8s` drie niveaus hoger zichtbaar, dan is `REPO_ROOT` optioneel (wordt afgeleid).
- Anders: in `deploy-config.local.env` **`REPO_ROOT`** zetten (bijv. `/opt/minicloud`) en **`GIT_REPO_URL`** (HTTPS met token of SSH) — bij eerste run wordt daarheen **geclone** (`GIT_REF`, standaard `develop`; shallow met `GIT_CLONE_DEPTH`).
- Bij **`TAG_MODE=latest`** is standaard **`UPDATE_GIT_BEFORE_DEPLOY=auto`** → vóór deploy wordt **`git fetch` / `checkout` / `pull --ff-only`** op `GIT_REF` uitgevoerd zodat manifests (workflows, k8s) gelijk lopen met wat je uitrolt; images blijven `:latest` uit de registry.

1. Kopieer de voorbeeldconfig en vul in (bestand staat **niet** in git):
   ```bash
   cp deploy/k8s/scripts/deploy-config.example.env deploy/k8s/scripts/deploy-config.local.env
   ```
2. Minimaal `REGISTRY_PREFIX`; op kale machine ook `REPO_ROOT` + `GIT_REPO_URL`; registry-credentials voor pull secret indien privé.
3. **Tag**: `latest`, `explicit` + `EXPLICIT_TAG`, of `git_short` (HEAD na git-sync moet in de registry bestaan als CI-tag).
4. Uitvoeren (pad naar script mag vanuit overal als `REPO_ROOT` / clone klopt):
   ```bash
   ./deploy/k8s/scripts/gitlab-deploy.sh
   ```

Opties: `UPDATE_GIT_BEFORE_DEPLOY=true/false/auto`, `DRY_RUN=true`, `PRINT_ONLY=true`, pull secret en deployment-patches zoals in het example-bestand.

---

## 5. Workflows ConfigMap

The overlay inherits the base **`configMapGenerator`** for workflow YAML under `deploy/k8s/workflows/`. After changing workflows:

```bash
kubectl apply -k deploy/k8s/overlays/gitlab
kubectl rollout restart deployment/orchestrator -n minicloud
```

---

## 6. Ingress and access

- Base **Ingress** is optional (`deploy/k8s/ingress.yaml`). It expects an **Ingress controller** (e.g. nginx) and class `nginx`.
- Without Ingress, expose the gateway:

```bash
kubectl port-forward -n minicloud svc/gateway 8080:8080
```

- Set **TLS** and **hostname** on the Ingress resource for production.

Internal service DNS (same namespace): `http://gateway:8080`, `http://orchestrator:8080`, etc.

---

## 7. Environment variables (production hints)

Review and override via Kustomize `configMapGenerator` / `patches` or separate Secrets:

| Area | Examples |
|------|----------|
| Gateway | `ORCHESTRATOR_URL`, optional `GATEWAY_ORCHESTRATION_ONLY` (default manifests: `true`; only `/v1/run*` public). No `TRANSFORMERS_URL` on gateway when orchestration-only. |
| Orchestrator | `TRANSFORMERS_URL`, `EGRESS_*`, `WORKFLOWS_DIR`; optional `HTTP_INVOCATION_TOKEN`, `SCHEDULE_INVOCATION_TOKEN` (Secrets). |
| Dashboard | `ORCHESTRATOR_URL`, optional `DASH_DEFAULT_LIMIT`, `DASH_TIMEOUT_SECONDS`. |
| HTTP workflow triggers | Optional `HTTP_INVOCATION_TOKEN` (**Secret**); gateway forwards `Authorization`. |
| Scheduled jobs | Optional `SCHEDULE_INVOCATION_TOKEN` (**Secret**); see `orchestrator-deployment.yaml`. |
| Egress hardening | `HTTP_EGRESS_ALLOWED_HOSTS`, `FTP_EGRESS_ALLOWED_HOSTS`, `SSH_EGRESS_ALLOWED_HOSTS`. |

Base Deployments already set in-cluster URLs for Compose-style names; they remain valid when all services run in the **same namespace**.

---

## 8. Verification checklist

1. `kubectl get pods -n minicloud` — all pods **Running**.
2. `kubectl logs -n minicloud deploy/gateway` — no repeated connection errors to orchestrator/transformers.
3. `curl -s http://127.0.0.1:8080/healthz` (via port-forward) — `{"status":"ok"}`.
4. (Optional) `kubectl port-forward -n minicloud svc/dashboard 8090:8080` and open `http://127.0.0.1:8090`.
5. Run a workflow, e.g. `POST /v1/run/minimal` on the gateway (see [README – Examples](../README.md#examples-curl)).

---

## 9. CI: deploy job (optional)

This repository only **builds** images. To **deploy** from GitLab CI you can add a stage that:

- Uses `kubectl` or **Helm** with a **protected** `KUBECONFIG` or cluster API token stored in **CI/CD variables**.
- Runs `kubectl apply -k deploy/k8s/overlays/gitlab` (or your environment-specific overlay).

Keep deploy credentials out of the image and out of the Git history; use masked/protected variables.

---

## Related links

- [Kubernetes (in depth)](kubernetes.md)
- [GitLab (repo + CI + registry)](gitlab.md)
- [README – Kubernetes](../README.md#kubernetes)
