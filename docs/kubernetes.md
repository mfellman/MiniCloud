# Kubernetes (diepgaand)

Dit document beschrijft de Kubernetes-artefacten in [`deploy/k8s/`](../deploy/k8s/) en hoe ze samenhangen met [Docker Compose](../docker-compose.yml).

---

## 1. Overzicht componenten

| Resource | Bestand(en) | Rol |
|----------|-------------|-----|
| ConfigMap `xslt-templates` | `configmap-templates.yaml` | Placeholder voor toekomstige vaste XSLT-templates (nu niet door services gemount; uitbreidbaar). |
| ConfigMap `minicloud-workflows` | gegenereerd door Kustomize uit `workflows/*.yaml` | Workflow-YAML als bestanden onder `/app/workflows` in de orchestrator-pod. |
| Deployment + Service `gateway` | `gateway-deployment.yaml`, `gateway-service.yaml` | Publieke API; env o.a. `ORCHESTRATOR_URL`, `XSLT_URL`. |
| Deployment + Service `orchestrator` | `orchestrator-deployment.yaml`, `orchestrator-service.yaml` | Laadt workflows; volume mount naar workflow-ConfigMap. |
| Deployment + Service `xslt` | `xslt-deployment.yaml`, `xslt-service.yaml` | Intern XSLT. |
| Deployment + Service `httpcall` | `httpcall-deployment.yaml`, `httpcall-service.yaml` | Intern HTTP-client. |
| Ingress `gateway` | `ingress.yaml` | Optioneel; vereist een **Ingress Controller** (bv. nginx). |

DNS binnen dezelfde namespace: services bereikbaar als `http://<service-naam>:8080` (poort zoals in de Service gedefinieerd).

---

## 2. Kustomization

Bestand: [`deploy/k8s/kustomization.yaml`](../deploy/k8s/kustomization.yaml).

- **`generatorOptions.disableNameSuffixHash: true`** — vaste ConfigMap-naam `minicloud-workflows` (anders wijzigt de naam bij inhoud, wat deployments lastiger maakt).
- **`configMapGenerator`** — verzamelt `deploy/k8s/workflows/*.yaml` tot één ConfigMap. **Let op**: wijzigingen in workflows vereisen `kubectl apply -k` en vaak een **rollout restart** van de orchestrator als alleen de ConfigMap verandert (nieuwe data wordt wel gemount, maar de app laadt workflows bij **startup**).

---

## 3. Images en tags

Deployments gebruiken bijvoorbeeld:

- `minicloud/gateway:latest`
- `minicloud/orchestrator:latest`
- `minicloud/xslt:latest`
- `minicloud/httpcall:latest`

`imagePullPolicy: IfNotPresent` is geschikt voor **lokale clusters** (kind/minikube) waar je images lokaal bouwt en laadt.

Voor een echt register: tag versies, zet `imagePullPolicy: IfNotPresent` of `Always` naar wens, en vervang `:latest` door versie-tags.

---

## 4. Script: `local-kind.sh`

Pad: [`deploy/k8s/local-kind.sh`](../deploy/k8s/local-kind.sh).

1. Bouwt alle vier images vanuit de repository-root.
2. Laadt ze in het kind-cluster (`KIND_CLUSTER_NAME`, default `minicloud`).
3. Voert `kubectl apply -k deploy/k8s` uit.

**Vereisten**: Docker, [kind](https://kind.sigs.k8s.io/), `kubectl`.

Voorbeeld:

```bash
kind create cluster --name minicloud
./deploy/k8s/local-kind.sh
```

Daarna bijvoorbeeld:

```bash
kubectl port-forward svc/gateway 8080:8080
curl -s http://127.0.0.1:8080/healthz
```

---

## 5. Ingress

[`ingress.yaml`](../deploy/k8s/ingress.yaml) gebruikt `spec.ingressClassName: nginx`. Je cluster moet een controller hebben die die class bedient (bijv. [ingress-nginx](https://github.com/kubernetes/ingress-nginx)).

- Zonder werkende Ingress blijft de gateway bereikbaar via **port-forward** of een **LoadBalancer**-Service (niet in de standaardmanifesten).
- Pas **host** of **TLS** aan naar je omgeving.

---

## 6. Orchestrator: workflows en geheimen

- **Workflows**: komen uit de gegenereerde ConfigMap; pad in de container: `/app/workflows`.
- **Optioneel token** voor `POST /invoke/scheduled`: in [`orchestrator-deployment.yaml`](../deploy/k8s/orchestrator-deployment.yaml) staat een voorbeeld-comment om `SCHEDULE_INVOCATION_TOKEN` uit een **Secret** te halen. Genereer een Secret en verwijder de comment om te activeren.

---

## 7. Netwerk en security (richtlijnen)

- Exposeer in productie alleen **gateway** (Ingress).
- Overweeg **NetworkPolicy**: alleen gateway → orchestrator; alleen orchestrator → xslt/httpcall; geen directe route van buiten naar xslt/httpcall/orchestrator.
- `httpcall` ondersteunt **`HTTP_ALLOWED_HOSTS`** om uitgaande hosts te beperken (zie [README – omgevingsvariabelen](../README.md#omgevingsvariabelen)).

---

## 8. Volgorde bij updates

1. Wijzig workflow-YAML onder `deploy/k8s/workflows/`.
2. `kubectl apply -k deploy/k8s`.
3. Herstart orchestrator indien nodig: `kubectl rollout restart deployment/orchestrator`.

---

## 9. Verschil met Docker Compose

| Onderwerp | Compose | Kubernetes |
|-----------|---------|------------|
| Workflows | Bind-mount `services/orchestrator/workflows` | ConfigMap uit `deploy/k8s/workflows/` |
| Service-DNS | servicenaam (bv. `orchestrator`) | zelfde principe in-cluster |
| Publieke poort | gateway `8080:8080` | Ingress of port-forward |

Terug naar [README – Kubernetes](../README.md#kubernetes).
