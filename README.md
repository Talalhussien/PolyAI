# PolyAI

## Setup

Create and activate a virtual environment from the repo root directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Your terminal prompt should now show `(.venv)`. Keep this environment active whenever you run any service.

See each service's README for how to configure and run it.

## Cluster bootstrap prerequisites

The cluster workflow expects the GitHub Actions secret
`GRAFANA_ADMIN_PASSWORD` to be configured before bootstrapping a cluster. It
creates the Kubernetes Secret used by the ArgoCD-managed kube-prometheus-stack
without committing the password to this repository.

The Terraform ingress module looks up an existing public `fursa.click` Route
53 hosted zone and an issued ACM certificate. The certificate must be in the
selected AWS region and cover the hostnames in
`infra/tf/tfvars/us-east-1.tfvars`.
