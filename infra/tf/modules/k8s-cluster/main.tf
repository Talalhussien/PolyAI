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
      Effect   = "Allow"
      Action   = ["ssm:PutParameter", "ssm:AddTagsToResource"]
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
  name                = "${var.cluster_name}-worker-asg"
  vpc_zone_identifier = var.public_subnet_ids
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
