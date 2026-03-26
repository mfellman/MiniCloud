# Persistent Storage met Longhorn (Traces)

De orchestrator slaat workflow-run traces op onder `/app/traces` (zie [`trace_store.py`](../services/orchestrator/app/trace_store.py)).
In de standaard Kubernetes-manifests wordt hiervoor een **`emptyDir`** gebruikt — dat betekent dat **alle traces verloren gaan** bij een pod-restart of -reschedule.

Met [Longhorn](https://longhorn.io/) als distributed block storage kun je traces persistent opslaan, zodat ze pod-herstarts, rolling updates en node-failures overleven.

---

## 1. Longhorn installeren

Longhorn vereist een ondersteund Kubernetes-cluster (v1.25+) met `open-iscsi` op de nodes.

```bash
# Via Helm (aanbevolen)
helm repo add longhorn https://charts.longhorn.io
helm repo update
helm install longhorn longhorn/longhorn \
  --namespace longhorn-system \
  --create-namespace \
  --set defaultSettings.defaultDataPath="/var/lib/longhorn"
```

Wacht tot alle pods in `longhorn-system` running zijn:

```bash
kubectl -n longhorn-system get pods -w
```

Longhorn registreert automatisch een **StorageClass** genaamd `longhorn`.

> **Tip:** De Longhorn UI is beschikbaar via `kubectl -n longhorn-system port-forward svc/longhorn-frontend 8000:80`.

---

## 2. PersistentVolumeClaim aanmaken

Maak een PVC aan die door de orchestrator pod wordt gebruikt:

```yaml
# deploy/k8s/traces-pvc.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: orchestrator-traces
  labels:
    app.kubernetes.io/name: minicloud
    app.kubernetes.io/component: orchestrator
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: longhorn
  resources:
    requests:
      storage: 1Gi          # pas aan op basis van verwacht trace-volume
```

```bash
kubectl apply -f deploy/k8s/traces-pvc.yaml
kubectl get pvc orchestrator-traces
```

---

## 3. Orchestrator Deployment aanpassen

Vervang het `emptyDir` volume door een verwijzing naar de PVC.

In [`deploy/k8s/orchestrator-deployment.yaml`](../deploy/k8s/orchestrator-deployment.yaml), wijzig de `volumes`-sectie:

```yaml
      volumes:
        - name: workflows
          configMap:
            name: minicloud-workflows
        - name: connections
          configMap:
            name: minicloud-connections
        # Was: emptyDir: {}
        - name: traces
          persistentVolumeClaim:
            claimName: orchestrator-traces
```

De bestaande `volumeMounts` hoeven **niet** te veranderen — het mount-pad `/app/traces` blijft hetzelfde.

> **Let op:** Het volume heeft `ReadWriteOnce` — het kan maar door één node tegelijk worden gemount. Dit is prima bij `replicas: 1`. Heb je meerdere replica's nodig, overweeg dan `ReadWriteMany` (vereist Longhorn NFS of een RWX-capable StorageClass).

---

## 4. Apply en verifieer

```bash
# PVC aanmaken
kubectl apply -f deploy/k8s/traces-pvc.yaml

# Deployment updaten
kubectl apply -k deploy/k8s/

# Rollout (als alleen het volume is gewijzigd)
kubectl rollout restart deployment/orchestrator
kubectl rollout status deployment/orchestrator

# Controleer of het volume correct is gemount
kubectl exec deploy/orchestrator -- df -h /app/traces
kubectl exec deploy/orchestrator -- ls -la /app/traces
```

---

## 5. Dashboard leest traces via de orchestrator API

Het dashboard haalt traces op via de orchestrator (`GET /traces`, `GET /traces/{id}`).
Er is **geen extra volume-mount nodig op de dashboard pod** — alle trace-data wordt via de HTTP-API geserveerd.

---

## 6. Backup en retentie

### Longhorn snapshots & backups

Longhorn biedt ingebouwde snapshot- en backup-functionaliteit:

```bash
# Maak een snapshot via de Longhorn UI of CLI
# Stel een recurring backup schedule in via het Longhorn dashboard
```

Zie [Longhorn Backup/Restore documentatie](https://longhorn.io/docs/latest/snapshots-and-backups/) voor S3-compatible backup targets.

### Trace-retentie in de applicatie

De orchestrator prunt automatisch oude traces op basis van `TRACES_MAX_RUNS` (standaard 200). Pas dit aan in de deployment:

```yaml
            - name: TRACES_MAX_RUNS
              value: "500"        # bewaar meer runs op persistent storage
```

---

## 7. Sizing richtlijnen

| Scenario | Runs/dag | Gem. trace grootte | Aanbevolen PVC |
|----------|----------|--------------------|----------------|
| Development / test | < 50 | ~10 KB | 256Mi |
| Productie (licht) | 50–200 | ~50 KB | 1Gi |
| Productie (zwaar) | 200–1000 | ~100 KB | 5Gi |

Houd rekening met `TRACES_MAX_RUNS` — bij 500 runs van elk 100 KB is ~50 MB nodig, plus overhead.

---

## 8. Alternatieve StorageClasses

Longhorn is een populaire keuze voor bare-metal en edge clusters, maar dezelfde PVC-aanpak werkt met elke CSI-compatible StorageClass:

| Provider | StorageClass | Opmerkingen |
|----------|--------------|-------------|
| Longhorn | `longhorn` | Distributed replicated storage, Longhorn UI |
| Rook-Ceph | `ceph-block` | Enterprise-grade, meer complex |
| OpenEBS (Mayastor) | `mayastor-single-replica` | Lightweight, NVMe-optimized |
| NFS Subdir Provisioner | `nfs-client` | Simpel, RWX mogelijk |
| Cloud (AWS EBS) | `gp3` | Managed, alleen cloud |
| Cloud (Azure Disk) | `managed-premium` | Managed, alleen cloud |

Vervang `storageClassName: longhorn` in de PVC door de gewenste class.

---

## 9. Troubleshooting

| Symptoom | Oorzaak | Oplossing |
|----------|---------|-----------|
| PVC blijft `Pending` | Longhorn niet geïnstalleerd of StorageClass bestaat niet | `kubectl get sc` en controleer Longhorn pods |
| Pod start niet (`FailedMount`) | PVC is al door een andere node gemount (RWO) | Schaal naar 1 replica of gebruik RWX |
| Traces verdwijnen na restart | Volume mount niet correct | Controleer `volumeMounts` en `volumes` in deployment |
| Disk vol | `TRACES_MAX_RUNS` te hoog of PVC te klein | Verlaag `TRACES_MAX_RUNS` of vergroot PVC (`kubectl edit pvc`) |
| Permission denied op `/app/traces` | `fsGroup` matcht niet | Controleer `securityContext.fsGroup: 10001` in de deployment |
