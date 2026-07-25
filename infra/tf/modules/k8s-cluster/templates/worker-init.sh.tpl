#!/bin/bash
set -euxo pipefail
exec > >(tee /var/log/k8s-init.log) 2>&1

# 1. Kubernetes requires swap off.
swapoff -a
sed -i '/ swap / s/^/#/' /etc/fstab

# 2. Kernel modules + sysctl required for pod networking.
cat <<EOF | tee /etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF
modprobe overlay
modprobe br_netfilter

cat <<EOF | tee /etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sysctl --system

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y apt-transport-https ca-certificates curl gnupg awscli

# 3. Install CRI-O (container runtime).
mkdir -p /etc/apt/keyrings
curl -fsSL https://pkgs.k8s.io/addons:/cri-o:/prerelease:/main/deb/Release.key |
  gpg --dearmor -o /etc/apt/keyrings/cri-o-apt-keyring.gpg
echo "deb [signed-by=/etc/apt/keyrings/cri-o-apt-keyring.gpg] https://pkgs.k8s.io/addons:/cri-o:/prerelease:/main/deb/ /" |
  tee /etc/apt/sources.list.d/cri-o.list
apt-get update -y
apt-get install -y cri-o
systemctl enable --now crio

# 4. Install kubelet, kubeadm, kubectl from the official Kubernetes apt repo.
curl -fsSL https://pkgs.k8s.io/core:/stable:/v${kubernetes_version}/deb/Release.key |
  gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v${kubernetes_version}/deb/ /" |
  tee /etc/apt/sources.list.d/kubernetes.list
apt-get update -y
apt-get install -y kubelet kubeadm kubectl
apt-mark hold kubelet kubeadm kubectl
systemctl enable kubelet

# 5. Idempotency guard: if this instance already joined (e.g. user data
# re-triggered), don't try to join again.
if [ -f /etc/kubernetes/kubelet.conf ]; then
  echo "Already joined, skipping kubeadm join."
  exit 0
fi

# 6. Wait for the control plane to publish a join command, then join.
# Workers may boot before or concurrently with the control plane finishing
# `kubeadm init`, so poll SSM with a bounded retry loop rather than assuming
# the parameter already exists.
JOIN_CMD=""
for i in $(seq 1 30); do
  JOIN_CMD=$(aws ssm get-parameter \
    --region "${aws_region}" \
    --name "${ssm_param_name}" \
    --with-decryption \
    --query "Parameter.Value" \
    --output text 2>/dev/null) && break
  echo "Join command not published yet, retrying in 15s ($i/30)..."
  sleep 15
done

if [ -z "$JOIN_CMD" ]; then
  echo "Timed out waiting for join command in SSM parameter ${ssm_param_name}" >&2
  exit 1
fi

eval "$JOIN_CMD"
