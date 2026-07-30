# ---------------------------------------------------------------------------
# AMI lookup — resolved per-region instead of hardcoding an AMI ID, so this
# module works in any region a workspace/tfvars pair points it at.
# ---------------------------------------------------------------------------
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# ---------------------------------------------------------------------------
# Security group — shared by control plane and workers.
# Kubernetes needs many ports open between nodes (API server 6443, kubelet
# 10250, etcd 2379-2380, CNI overlay...); rather than enumerate each one,
# allowing all traffic within the trusted VPC CIDR is the pragmatic choice
# for this lab cluster.
# ---------------------------------------------------------------------------
resource "aws_security_group" "cluster" {
  name        = "${var.cluster_name}-sg"
  description = "kubeadm cluster: SSH + all intra-VPC traffic"
  vpc_id      = var.vpc_id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.allowed_ssh_cidr]
  }

  ingress {
    description = "All traffic between cluster nodes"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }

  # App NodePorts — frontend, agent, yolo, img-proc-mcp, prometheus, grafana.
  # Opens these exact port numbers for future type: NodePort Services in
  # infra/k8s (requires widening kube-apiserver's --service-node-port-range,
  # since the K8s default only allows 30000-32767).
  dynamic "ingress" {
    for_each = toset([3000, 3001, 8000, 8080, 9000, 9090])
    content {
      description = "App NodePort ${ingress.value}"
      from_port   = ingress.value
      to_port     = ingress.value
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.cluster_name}-sg" }
}

# ---------------------------------------------------------------------------
# IAM — control plane
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "ec2_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "control_plane" {
  # IAM role names are global to the AWS account (not per-region), so the
  # region is baked in here to avoid a name collision if the same
  # cluster_name is ever reused in a second workspace/region.
  name               = "${var.cluster_name}-${var.aws_region}-control-plane-role"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
}

resource "aws_iam_role_policy_attachment" "cp_eks_cluster" {
  role       = aws_iam_role.control_plane.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_iam_role_policy_attachment" "cp_ebs_csi" {
  role       = aws_iam_role.control_plane.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

resource "aws_iam_role_policy_attachment" "cp_ecr_ro" {
  role       = aws_iam_role.control_plane.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# Not covered by the managed policies above: permission to publish the join
# command that workers read at boot. SSM Parameter Store was chosen over
# Lambda/Secrets Manager/lifecycle hooks because it needs no extra service —
# the control plane writes once, workers poll-and-retry, and IAM scopes the
# read/write to this cluster's own parameter path.
resource "aws_iam_role_policy" "cp_ssm_write" {
  name = "${var.cluster_name}-cp-ssm-write"
  role = aws_iam_role.control_plane.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      # ssm:DeleteParameter added so the control plane can clear a stale
      # join-command parameter left over from a previous cluster generation
      # before publishing its own — see control-plane-init.sh.tpl.
      Action   = ["ssm:PutParameter", "ssm:AddTagsToResource", "ssm:DeleteParameter"]
      Resource = "arn:aws:ssm:${var.aws_region}:*:parameter/${var.cluster_name}/*"
    }]
  })
}

resource "aws_iam_instance_profile" "control_plane" {
  # Instance profile names are also global to the account — same reasoning
  # as aws_iam_role.control_plane above.
  name = "${var.cluster_name}-${var.aws_region}-control-plane-profile"
  role = aws_iam_role.control_plane.name
}

# ---------------------------------------------------------------------------
# IAM — workers
# ---------------------------------------------------------------------------
resource "aws_iam_role" "worker" {
  # See the comment on aws_iam_role.control_plane above — region-qualified
  # to stay unique across workspaces sharing the same cluster_name.
  name               = "${var.cluster_name}-${var.aws_region}-worker-role"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
}

resource "aws_iam_role_policy_attachment" "worker_ecr_ro" {
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# Minimum required worker policy per the course material — previously missing.
resource "aws_iam_role_policy_attachment" "worker_eks_worker_node" {
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

# The EBS CSI driver's node plugin runs as a DaemonSet on every node,
# including workers — previously this was only attached to the control-plane
# role above, which isn't enough once Prometheus's PV is actually mounted on
# a worker.
resource "aws_iam_role_policy_attachment" "worker_ebs_csi" {
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

resource "aws_iam_role_policy" "worker_ssm_read" {
  name = "${var.cluster_name}-worker-ssm-read"
  role = aws_iam_role.worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = "arn:aws:ssm:${var.aws_region}:*:parameter/${var.cluster_name}/*"
    }]
  })
}

resource "aws_iam_instance_profile" "worker" {
  name = "${var.cluster_name}-${var.aws_region}-worker-profile"
  role = aws_iam_role.worker.name
}

# Least-privilege S3 access for the app pods (YOLO/Agent/img-proc-mcp) that
# run on workers. There's no per-pod AWS identity on this self-managed
# (non-EKS) cluster, only the node's instance profile — so this grant is on
# the worker role, not a Kubernetes ServiceAccount. Split into two
# statements because ListBucket is a bucket-level action (targets the bucket
# ARN itself) while the object actions target keys inside it (bucket ARN +
# "/*") — combining them under one Resource list would apply the wrong scope
# to one or the other.
resource "aws_iam_role_policy" "worker_s3_access" {
  name = "${var.cluster_name}-${var.aws_region}-worker-s3-access"
  role = aws_iam_role.worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ObjectAccess"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.images.arn}/*"
      },
      {
        Sid      = "ListBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.images.arn
      }
    ]
  })
}

# The "agent" pod's MODEL env var (infra/k8s/{dev,prod}/deployment/agent-deployment.yaml)
# is "bedrock_converse/amazon.nova-lite-v1:0" — it calls AWS Bedrock directly,
# authenticating via the worker's instance profile (Bedrock has no separate
# API-key auth). Scoped to only the one model actually referenced.
resource "aws_iam_role_policy" "worker_bedrock_access" {
  name = "${var.cluster_name}-${var.aws_region}-worker-bedrock-access"
  role = aws_iam_role.worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      Resource = "arn:aws:bedrock:${var.aws_region}::foundation-model/amazon.nova-lite-v1:0"
    }]
  })
}

# ---------------------------------------------------------------------------
# Control plane EC2 instance
# ---------------------------------------------------------------------------
resource "aws_instance" "control_plane" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.control_plane_instance_type
  subnet_id              = var.public_subnet_ids[0]
  key_name               = var.key_pair_name
  vpc_security_group_ids = [aws_security_group.cluster.id]
  iam_instance_profile   = aws_iam_instance_profile.control_plane.name

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }

  user_data = templatefile("${path.module}/templates/control-plane-init.sh.tpl", {
    kubernetes_version = var.kubernetes_version
    ssm_param_name     = "/${var.cluster_name}/join-command"
    aws_region         = var.aws_region
  })

  tags = { Name = "${var.cluster_name}-control-plane" }
}

# ---------------------------------------------------------------------------
# Worker Launch Template
# ---------------------------------------------------------------------------
resource "aws_launch_template" "worker" {
  name_prefix   = "${var.cluster_name}-worker-"
  image_id      = data.aws_ami.ubuntu.id
  instance_type = var.worker_instance_type
  key_name      = var.key_pair_name

  iam_instance_profile {
    name = aws_iam_instance_profile.worker.name
  }

  vpc_security_group_ids = [aws_security_group.cluster.id]

  block_device_mappings {
    device_name = "/dev/sda1"
    ebs {
      volume_size = 20
      volume_type = "gp3"
    }
  }

  user_data = base64encode(templatefile("${path.module}/templates/worker-init.sh.tpl", {
    kubernetes_version = var.kubernetes_version
    ssm_param_name     = "/${var.cluster_name}/join-command"
    aws_region         = var.aws_region
  }))

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${var.cluster_name}-worker" }
  }
}

# ---------------------------------------------------------------------------
# Worker Auto Scaling Group
#
# Scale-down note: when the ASG terminates a worker instance, its Kubernetes
# Node object is NOT automatically removed (nothing links "EC2 instance
# terminated" to "delete this Node"), so it lingers as NotReady. This module
# does not implement ASG lifecycle-hook automation to clean it up, since that
# needs a Lambda + SNS/EventBridge + cluster-access chain beyond this
# course's material. Accepted manual cleanup after scaling down:
#   kubectl delete node <node-name>
# ---------------------------------------------------------------------------
resource "aws_autoscaling_group" "worker" {
  name = "${var.cluster_name}-worker-asg"
  # Pinned to the same single subnet/AZ as the Prometheus EBS volumes
  # (aws_ebs_volume.prometheus_{dev,prod}, azs[1]) — NOT necessarily the same
  # AZ as the control plane, which stays on public_subnet_ids[0] regardless;
  # control-plane/worker cross-AZ traffic is fine (same VPC, SG already
  # allows all intra-VPC traffic). EBS volumes can't attach across AZs, so a
  # worker landing in a different AZ than the volumes would make the
  # Prometheus pod fail to mount its volume. Trade-off: workers no longer
  # spread across both AZs — acceptable for this single-worker lab cluster;
  # revisit if worker count/resilience needs grow beyond what one AZ can offer.
  vpc_zone_identifier = [var.public_subnet_ids[1]]
  min_size            = var.worker_min_size
  max_size            = var.worker_max_size
  desired_capacity    = var.worker_desired_capacity
  health_check_type   = "EC2"

  launch_template {
    id      = aws_launch_template.worker.id
    version = "$Latest"
  }

  tag {
    key                 = "Name"
    value               = "${var.cluster_name}-worker"
    propagate_at_launch = true
  }

  # Workers must never launch before the control plane exists (they'd have
  # nothing to join), so make the ordering explicit even though the SSM
  # retry loop in worker-init.sh.tpl already tolerates the race.
  depends_on = [aws_instance.control_plane]
}

# ---------------------------------------------------------------------------
# S3 bucket — application images (read/written by the YOLO/Agent/img-proc-mcp
# pods via the worker role's IAM permissions above)
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "images" {
  bucket = var.s3_bucket_name

  tags = {
    Name    = var.s3_bucket_name
    Project = var.cluster_name
  }
}

# Blocks all public access regardless of any bucket policy or (legacy) ACL —
# this bucket is only ever read/written by the worker role, never the public.
resource "aws_s3_bucket_public_access_block" "images" {
  bucket = aws_s3_bucket.images.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versioning gives a recovery path against the s3:DeleteObject/PutObject
# permission just granted to the worker role — an accidental overwrite or
# delete from a buggy pod doesn't lose the previous object version.
resource "aws_s3_bucket_versioning" "images" {
  bucket = aws_s3_bucket.images.id
  versioning_configuration {
    status = "Enabled"
  }
}

# ---------------------------------------------------------------------------
# Prometheus EBS volumes — dev and prod.
#
# Previously created manually and referenced by hardcoded volumeHandle in
# infra/k8s/{dev,prod}/pv/prometheus-pv.yaml. Tracking them here means they're
# reproducible from code, but it does NOT retroactively change those existing
# PV manifests or migrate data off the manually-created volumes — that's a
# separate, deliberate follow-up (see the plan's "manual steps" section).
#
# Both volumes are pinned to azs[1] (us-east-1b), matching the worker ASG's
# vpc_zone_identifier above — EBS volumes are AZ-locked, so the worker and
# these volumes must always agree on which AZ they're in. The control plane
# is unaffected — it stays on azs[0]/public_subnet_ids[0] independently.
# ---------------------------------------------------------------------------
resource "aws_ebs_volume" "prometheus_dev" {
  availability_zone = var.azs[1]
  size              = 5
  type              = "gp3"

  tags = {
    Name        = "prometheus-data-dev"
    Project     = "PolyAI"
    Environment = "dev"
  }
}

resource "aws_ebs_volume" "prometheus_prod" {
  availability_zone = var.azs[1]
  size              = 5
  type              = "gp3"

  tags = {
    Name        = "prometheus-data-prod"
    Project     = "PolyAI"
    Environment = "prod"
  }
}

# ---------------------------------------------------------------------------
# Kubernetes cluster add-ons NOT managed here — documented, not implemented.
#
# This configuration has no "kubernetes" or "helm" Terraform provider (only
# "aws", declared in the root main.tf), and no kubeconfig is wired into this
# Terraform run. Managing cluster add-ons from here would mean adding a new
# provider and giving this Terraform run network access to the control
# plane's API server — a real design change, not a minimal one, so it's left
# as a manual step instead:
#
#   EBS CSI driver (required for the ebs.csi.aws.com StorageClass/PVs in
#   infra/k8s/storage/ and infra/k8s/{dev,prod}/pv/ to do anything):
#     kubectl apply -k "github.com/kubernetes-sigs/aws-ebs-csi-driver/deploy/kubernetes/overlays/stable/?ref=release-1.31"
#
#   metrics-server (required for the 3 HPAs in infra/k8s/{dev,prod}/hpa/ to
#   read CPU% — without it they show <unknown> and never scale):
#     kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
#
# Both are one-time, run-once-per-cluster steps after `terraform apply` and
# after kubeadm join has completed for at least one worker.
# ---------------------------------------------------------------------------
