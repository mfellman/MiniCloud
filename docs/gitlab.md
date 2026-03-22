# GitLab (repository + CI + registry)

Short guide to host this project on **GitLab** and build images automatically.

---

## 1. Connect the project and first push

### 1.1 Your GitLab project URL

1. Open your project on GitLab.
2. Click **Code** (or **Clone**) and copy the **HTTPS** or **SSH** URL, for example:
   - HTTPS: `https://gitlab.com/your-group/minicloud.git`
   - SSH: `git@gitlab.com:your-group/minicloud.git`

### 1.2 Locally: add remote and push

In this project directory (if `git init` and a commit already exist):

```bash
cd /path/to/MiniCloud
git remote add origin https://gitlab.com/<group>/<project>.git
git push -u origin main
```

- **First time HTTPS**: GitLab asks for login. Use your **username** and a **Personal Access Token** as the password (not your account password if 2FA is on):  
  **Edit profile → Access Tokens** → scope **`write_repository`**.
- **SSH**: add your public key under **Edit profile → SSH Keys**, then use the SSH URL with `git remote add`.

### 1.3 Empty project vs project with README

| Situation | What to do |
|-----------|------------|
| **Empty project** (GitLab says “push an existing repository”) | `git push -u origin main` after `git remote add` is enough. |
| **Project created with README/license** | Fetch and merge first: `git pull origin main --allow-unrelated-histories`, resolve conflicts, then `git push -u origin main`. |

### 1.4 Git username (one-time on your machine)

Set name and email for commits (recommended):

```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

If this repo already has a first commit with a temporary author, you can amend later with `git commit --amend` or rely on the config above for new commits.

### 1.5 From scratch (no git repo yet)

```bash
git init -b main
git add .
git commit -m "Initial commit"
git remote add origin https://gitlab.com/<group>/<project>.git
git push -u origin main
```

---

## 2. Enable the Container Registry

1. **Settings → General → Visibility, project features, permissions**
2. Turn **Container Registry** on (often on by default on gitlab.com).
3. Your registry base is then:

   `registry.gitlab.com/<group>/<project>`

   Each service image is pushed as:

   - `registry.gitlab.com/<group>/<project>/gateway:<tag>`
   - `.../transformers:<tag>`
   - `.../egress-http:<tag>`
   - `.../egress-ftp:<tag>`
   - `.../egress-ssh:<tag>`
   - `.../orchestrator:<tag>`

   Tags: `latest` and the short commit SHA (`CI_COMMIT_SHORT_SHA`), see [`.gitlab-ci.yml`](../.gitlab-ci.yml).

---

## 3. CI/CD pipeline

The **[`.gitlab-ci.yml`](../.gitlab-ci.yml)** file lives in the repo root. It uses **Kaniko** (no Docker-in-Docker) and builds service images in parallel.

- On every **push to a branch** or **tag**, build jobs run (see `rules` in the YAML).
- On **gitlab.com shared runners** this usually works out of the box; on **self-managed GitLab** you need runners and outbound internet (Kaniko pulls base images).

First run: check **CI/CD → Pipelines**; on failure read job logs (often registry auth or network).

---

## 4. Kubernetes: use GitLab images

Manifests under `deploy/k8s/` currently reference `minicloud/<service>:latest`. For a **step-by-step full-stack deploy** (namespace, pull secrets, Kustomize overlay, verification), see **[Deploy on Kubernetes (GitLab Registry)](deployment-kubernetes-gitlab.md)**.

For a short manual image override:

**Option A — manual (one-off):**

```bash
export REG=registry.gitlab.com/<group>/<project>
kubectl set image deployment/gateway gateway=${REG}/gateway:latest -n <ns>
# repeat for transformers, egress-http, egress-ftp, egress-ssh, orchestrator
```

**Option B — Kustomize `images:`** in an overlay (recommended for repeatable deploys). The repo includes [`deploy/k8s/overlays/gitlab/kustomization.yaml`](../deploy/k8s/overlays/gitlab/kustomization.yaml); edit the registry prefix and run `kubectl apply -k deploy/k8s/overlays/gitlab` (details in [deployment-kubernetes-gitlab.md](deployment-kubernetes-gitlab.md)).

```yaml
# e.g. deploy/k8s/overlays/gitlab/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../  # or point to your base
images:
  - name: minicloud/gateway
    newName: registry.gitlab.com/<group>/<project>/gateway
    newTag: latest
  - name: minicloud/transformers
    newName: registry.gitlab.com/<group>/<project>/transformers
    newTag: latest
  - name: minicloud/egress-http
    newName: registry.gitlab.com/<group>/<project>/egress-http
    newTag: latest
  - name: minicloud/egress-ftp
    newName: registry.gitlab.com/<group>/<project>/egress-ftp
    newTag: latest
  - name: minicloud/egress-ssh
    newName: registry.gitlab.com/<group>/<project>/egress-ssh
    newTag: latest
  - name: minicloud/orchestrator
    newName: registry.gitlab.com/<group>/<project>/orchestrator
    newTag: latest
```

The cluster must be able to pull from the registry: **deploy token**, **robot account**, or **imagePullSecrets**. See [GitLab: Authenticate with container registry](https://docs.gitlab.com/ee/user/packages/container_registry/authenticate_with_container_registry.html).

---

## 5. What GitLab does not do for you

- The **application** (pods + services) does not run “on GitLab”; it runs on **your Kubernetes** (or elsewhere). GitLab mainly provides **git**, **CI**, and **registry**.
- **Deploy to K8s** can be added later as a separate CI job (`kubectl`, Helm, Flux, etc.).

---

## 6. Back to overview

- [README – additional documentation](../README.md#additional-documentation)
- [Kubernetes (in depth)](kubernetes.md)
