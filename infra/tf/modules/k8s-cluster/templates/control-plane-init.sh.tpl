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
apt-get install -y apt-transport-https ca-certificates curl gnupg unzip

# Install AWS CLI v2 via the official installer — not `apt-get install awscli`,
# which pulls Ubuntu's packaged v1 and its large, unrelated dependency chain
# (ImageMagick, fonts, PIL, etc.) that has nothing to do with this script.
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip -q awscliv2.zip
./aws/install
rm -rf awscliv2.zip aws/

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

# 5. Initialize the control plane.
LOCAL_IP=$(curl -s http://169.254.169.254/latest/meta-data/local-ipv4)
kubeadm init \
  --pod-network-cidr=192.168.0.0/16 \
  --apiserver-advertise-address="$LOCAL_IP"

# 6. Let the ubuntu user run kubectl without sudo.
mkdir -p /home/ubuntu/.kube
cp -i /etc/kubernetes/admin.conf /home/ubuntu/.kube/config
chown ubuntu:ubuntu /home/ubuntu/.kube/config

# 7. Publish a join command workers can consume.
# --ttl 0 makes the token non-expiring: new workers can join weeks after
# `kubeadm init` ran (e.g. after scaling the ASG back up), not just within
# the default 24h token lifetime. Acceptable for this course project; a
# production cluster would rotate tokens instead of using a permanent one.
JOIN_CMD=$(kubeadm token create --ttl 0 --print-join-command)
aws ssm put-parameter \
  --region "${aws_region}" \
  --name "${ssm_param_name}" \
  --type SecureString \
  --value "$JOIN_CMD" \
  --overwrite
