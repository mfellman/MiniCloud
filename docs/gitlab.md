# GitLab (repository + CI + registry)

Korte gids voor het project op **GitLab** te zetten en images automatisch te bouwen.

---

## 1. Project koppelen en eerste push

### 1.1 URL van je GitLab-project

1. Open je project op GitLab.
2. Klik op **Code** (of **Clone**) en kopieer de **HTTPS**- of **SSH**-URL, bijvoorbeeld:
   - HTTPS: `https://gitlab.com/jouw-groep/minicloud.git`
   - SSH: `git@gitlab.com:jouw-groep/minicloud.git`

### 1.2 Lokaal: remote toevoegen en pushen

In de map van dit project (als hier al `git init` en een commit zijn gedaan):

```bash
cd /pad/naar/MiniCloud
git remote add origin https://gitlab.com/<groep>/<project>.git
git push -u origin main
```

- **Eerste keer HTTPS**: GitLab vraagt om inloggen. Gebruik je **gebruikersnaam** en een **Personal Access Token** als wachtwoord (niet je accountwachtwoord als je 2FA aan hebt):  
  **Edit profile → Access Tokens** → scope **`write_repository`**.
- **SSH**: voeg je publieke sleutel toe onder **Edit profile → SSH Keys**, gebruik dan de SSH-URL bij `git remote add`.

### 1.3 Leeg project vs. project met README

| Situatie | Wat te doen |
|----------|-------------|
| **Leeg project** (GitLab zegt “push an existing repository”) | `git push -u origin main` na `git remote add` volstaat. |
| **Project met README/licentie** aangemaakt | Eerst ophalen en samenvoegen: `git pull origin main --allow-unrelated-histories`, conflicten oplossen, daarna `git push -u origin main`. |

### 1.4 Git-gebruikersnaam (eenmalig op je machine)

Zet je naam en e-mail voor commits (aanbevolen):

```bash
git config --global user.name "Jouw Naam"
git config --global user.email "jouw@email"
```

Als dit project al een eerste commit heeft met een tijdelijke auteur, kun je die later aanpassen met `git commit --amend` of voor nieuwe commits volstaat bovenstaande config.

### 1.5 Vanaf nul (als je nog geen git-repo hebt)

```bash
git init -b main
git add .
git commit -m "Initial commit"
git remote add origin https://gitlab.com/<groep>/<project>.git
git push -u origin main
```

---

## 2. Container Registry inschakelen

1. **Settings → General → Visibility, project features, permissions**
2. Zet **Container Registry** aan (vaak standaard aan op gitlab.com).
3. Je registry-basis is dan:

   `registry.gitlab.com/<groep>/<project>`

   Elke service wordt gepusht als:

   - `registry.gitlab.com/<groep>/<project>/gateway:<tag>`
   - `.../xslt:<tag>`
   - `.../httpcall:<tag>`
   - `.../orchestrator:<tag>`

   Tags: `latest` en de **korte commit-SHA** (`CI_COMMIT_SHORT_SHA`), zie [`.gitlab-ci.yml`](../.gitlab-ci.yml).

---

## 3. CI/CD-pipeline

Het bestand **[`.gitlab-ci.yml`](../.gitlab-ci.yml)** staat in de root. Het gebruikt **Kaniko** (geen Docker-in-Docker nodig) en bouwt de vier images parallel.

- Bij elke **push naar een branch** of **tag** draaien de build-jobs (zie `rules` in het YAML).
- Op **gitlab.com shared runners** werkt dit doorgaans direct; op **eigen GitLab** moeten runners beschikbaar zijn en outbound internet toestaan (Kaniko haalt base images op).

Eerste run: controleer **CI/CD → Pipelines**; bij falen de joblogs bekijken (vaak registry-auth of netwerk).

---

## 4. Kubernetes: images uit GitLab gebruiken

De manifests onder `deploy/k8s/` verwijzen nu naar `minicloud/<service>:latest`. Voor productie vervang je dat door je registry-paden.

**Optie A — handmatig (één keer):**

```bash
export REG=registry.gitlab.com/<groep>/<project>
kubectl set image deployment/gateway gateway=${REG}/gateway:latest -n <ns>
# herhaal voor xslt, httpcall, orchestrator
```

**Optie B — Kustomize `images:`** in een overlay (aanbevolen voor herhaalbare deploys):

```yaml
# bijv. deploy/k8s/overlays/gitlab/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../  # of verwijs naar je base
images:
  - name: minicloud/gateway
    newName: registry.gitlab.com/<groep>/<project>/gateway
    newTag: latest
  - name: minicloud/xslt
    newName: registry.gitlab.com/<groep>/<project>/xslt
    newTag: latest
  - name: minicloud/httpcall
    newName: registry.gitlab.com/<groep>/<project>/httpcall
    newTag: latest
  - name: minicloud/orchestrator
    newName: registry.gitlab.com/<groep>/<project>/orchestrator
    newTag: latest
```

Het cluster moet de registry kunnen pullen: **Deploy token** of **robot account**, of imagePullSecrets. Zie [GitLab: Authenticate with container registry](https://docs.gitlab.com/ee/user/packages/container_registry/authenticate_with_container_registry.html).

---

## 5. Wat GitLab níet voor je doet

- De **applicatie zelf** (vier pods + services) draait niet “op GitLab”; die draait op **jouw Kubernetes** (of elders). GitLab levert vooral **git**, **CI**, en **registry**.
- **Deploy naar K8s** kun je later als aparte CI-job toevoegen (`kubectl`, Helm, Flux, enz.).

---

## 6. Terug naar overzicht

- [README – aanvullende documentatie](../README.md#aanvullende-documentatie)
- [Kubernetes (diepgaand)](kubernetes.md)
