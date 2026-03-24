# GitLab registry overlay

Replace `registry.gitlab.com/example-group/example-project` in `kustomization.yaml` with your GitLab **`CI_REGISTRY_IMAGE`** value, then:

```bash
kubectl apply -k deploy/k8s/overlays/gitlab
```

Full steps: [docs/deployment-kubernetes-gitlab.md](../../../docs/deployment-kubernetes-gitlab.md).

For a **tunable rollout** from a controller machine (generated overlay + optional pull secret), see [`deploy/k8s/scripts/gitlab-deploy.sh`](../../scripts/gitlab-deploy.sh).
