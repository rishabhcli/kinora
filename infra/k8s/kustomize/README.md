# Kinora — Kustomize manifests

A raw-manifest alternative to the Helm chart (`../helm/kinora`) for GitOps shops
(Argo CD / Flux) that prefer Kustomize over Helm. The **Helm chart is the source
of truth**; this base is a hand-maintained, dependency-free mirror of the same
six-role process model so a cluster can be stood up with just `kubectl` /
`kustomize` and no templating engine.

```
base/        the namespace + ConfigMap + Secret + the five role Deployments,
             Services, the MCP-intra-app NetworkPolicy, and the migrate Job
overlays/
  dev/       single replicas, APP_ENV=dev, KINORA_LIVE_VIDEO=false
  prod/      HA replicas, APP_ENV=production, NetworkPolicies, resource bumps
```

Validate / render:

```bash
kubectl kustomize overlays/dev   | kubeconform -strict -ignore-missing-schemas -summary -
kubectl kustomize overlays/prod  | kubeconform -strict -ignore-missing-schemas -summary -
```

> The chart-managed Secret in `base/secret.yaml` carries placeholder values only.
> In prod, replace it with an External Secrets Operator `ExternalSecret` or a
> Vault-synced Secret — never commit real credentials.
</content>
