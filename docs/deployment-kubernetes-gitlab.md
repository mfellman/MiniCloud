# Deploy MiniCloud on Kubernetes with GitLab Container Registry

This guide describes how to run the **entire** MiniCloud stack on a Kubernetes cluster using images built and stored in the **GitLab Container Registry** (typically via the included [`.gitlab-ci.yml`](../.gitlab-ci.yml)).

For cluster concepts (ConfigMaps, Ingress, `local-kind.sh`), see [Kubernetes (in depth)](kubernetes.md). For GitLab project setup and CI overview, see [GitLab (repo + CI + registry)](gitlab.md).

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
2. In **Deploy → Container Registry**, confirm images such as `gateway`, `transformers`, etc.
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
for d in gateway orchestrator transformers egress-http egress-ftp egress-ssh; do
  kubectl patch deployment "$d" -n "$NS" --type merge -p \
    '{"spec":{"template":{"spec":{"imagePullSecrets":[{"name":"gitlab-registry"}]}}}}'
done
```

Alternatively, integrate a **Kustomize strategic merge** or **JSON6902 patch** in your own overlay if you prefer GitOps-only workflows.

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
| Gateway | `ORCHESTRATOR_URL`, `TRANSFORMERS_URL` — use in-cluster URLs (`http://orchestrator:8080`). |
| Orchestrator | `TRANSFORMERS_URL`, `EGRESS_HTTP_URL`, `EGRESS_FTP_URL`, `EGRESS_SSH_URL`, `WORKFLOWS_DIR` (default `/app/workflows` when using ConfigMap mount). |
| Scheduled jobs | `SCHEDULE_INVOCATION_TOKEN` from a **Secret** (see comments in `orchestrator-deployment.yaml`). |
| Egress hardening | `HTTP_EGRESS_ALLOWED_HOSTS`, `FTP_EGRESS_ALLOWED_HOSTS`, `SSH_EGRESS_ALLOWED_HOSTS`. |

Base Deployments already set in-cluster URLs for Compose-style names; they remain valid when all services run in the **same namespace**.

---

## 8. Verification checklist

1. `kubectl get pods -n minicloud` — all pods **Running**.
2. `kubectl logs -n minicloud deploy/gateway` — no repeated connection errors to orchestrator/transformers.
3. `curl -s http://127.0.0.1:8080/healthz` (via port-forward) — `{"status":"ok"}`.
4. Run a workflow, e.g. `POST /v1/run/minimal` on the gateway (see [README – Examples](../README.md#examples-curl)).

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
