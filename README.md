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

The cluster workflow can optionally receive the GitHub Actions secret
`GRAFANA_ADMIN_PASSWORD`. If it is not configured, bootstrap generates a
random password on the control plane and creates the Kubernetes Secret used by
the ArgoCD-managed kube-prometheus-stack without committing the password to
this repository. Existing Grafana Secrets are preserved on reruns.

The Terraform ingress module looks up an existing public `fursa.click` Route
53 hosted zone and an issued ACM certificate. The persistent certificate stack
in `infra/tf/certificate` creates and validates
`*.talalhuss.fursa.click` plus `talalhuss.fursa.click`. Run that stack before
the cluster stack; it uses a separate state file so destroying the cluster does
not destroy the certificate.

The GitHub Actions role also needs the Route 53, ACM read, and ALB permissions
used by those stacks. They are managed separately in
`infra/tf/bootstrap-iam`, using the state key
`github-terraform-permissions.tfstate`. The provisioning workflow applies this
stack before the certificate and cluster stacks.
