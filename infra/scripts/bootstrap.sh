#!/bin/bash
# Idempotent cluster bootstrap: Calico, EBS CSI driver, metrics-server,
# ArgoCD, and the 12 ArgoCD Applications. Runs once per cluster lifetime via
# cluster.yaml's Bootstrap job (over SSH), but safe to re-run — every step below is
# `kubectl apply`-based (not `create`), so re-running after a partial
# failure or a manual re-trigger converges rather than erroring out.
#
# Expects DEV_VOL and PROD_VOL as environment variables (the fresh EBS
# volume IDs from this run's `terraform output`), and expects
# infra/k8s/ + infra/argocd/ to already be present in the working
# directory (scp'd up by the calling workflow before this runs).
set -euxo pipefail

echo "Waiting for cloud-init to finish..."
until sudo cloud-init status --wait >/dev/null 2>&1; do sleep 5; done

echo "Waiting for the Kubernetes API to respond..."
until kubectl get --raw='/healthz' >/dev/null 2>&1; do sleep 5; done

echo "Waiting for both nodes to register..."
until [ "$(kubectl get nodes --no-headers 2>/dev/null | wc -l)" -ge 2 ]; do sleep 10; done

echo "Installing Calico..."
kubectl apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.28.0/manifests/calico.yaml

echo "Waiting for both nodes to reach Ready..."
until [ "$(kubectl get nodes --no-headers 2>/dev/null | grep -c ' Ready ')" -ge 2 ]; do sleep 10; done

echo "Installing the EBS CSI driver..."
kubectl apply -k 'github.com/kubernetes-sigs/aws-ebs-csi-driver/deploy/kubernetes/overlays/stable/?ref=release-1.31'

echo "Installing metrics-server..."
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml

echo "Installing ArgoCD..."
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/v3.4.5/manifests/install.yaml --server-side --force-conflicts

echo "Waiting for ArgoCD pods..."
kubectl -n argocd wait --for=condition=Available --timeout=300s deployment --all

echo "Applying namespaces, storage class, network policies..."
kubectl apply -f k8s/namespaces/
kubectl apply -f k8s/storage/


echo "Applying Prometheus PVs with this run's live volume IDs..."
: "${DEV_VOL:?DEV_VOL must be set}"
: "${PROD_VOL:?PROD_VOL must be set}"
sed -E "s|^( *volumeHandle: ).*|\1${DEV_VOL}|"  k8s/dev/pv/prometheus-pv.yaml  | kubectl apply -f -
sed -E "s|^( *volumeHandle: ).*|\1${PROD_VOL}|" k8s/prod/pv/prometheus-pv.yaml | kubectl apply -f -

echo "Creating ArgoCD Applications..."
kubectl apply -f argocd/dev/
kubectl apply -f argocd/prod/

echo "Bootstrap complete."
kubectl get nodes
kubectl get applications -n argocd
