# ---------------------------------------------------------------------------
# AMI lookup — resolved per-region instead of hardcoding an AMI ID, so this
# module works in any region a workspace/tfvars pair points it at.
# ---------------------------------------------------------------------------
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-resolute-26.04-amd64-server-*"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# ---------------------------------------------------------------------------
# Security group — shared by control plane and workers.
# Kubernetes needs many ports open between nodes (API server 6443, kubelet
# 10250, etcd 2379-2380, CNI overlay...)
# ---------------------------------------------------------------------------
resource "aws_security_group" "cluster" {
  name        = "${var.cluster_name}-sg"
  description = "kubeadm cluster: SSH + all intra-VPC traffic"
  vpc_id      = var.vpc_id

  tags = { Name = "${var.cluster_name}-sg" }
}

resource "aws_security_group_rule" "cluster_ssh" {
  type              = "ingress"
  security_group_id = aws_security_group.cluster.id
  description       = "SSH"
  from_port         = 22
  to_port           = 22
  protocol          = "tcp"
  cidr_blocks       = [var.allowed_ssh_cidr]
}

resource "aws_security_group_rule" "cluster_intra_vpc" {
  type              = "ingress"
  security_group_id = aws_security_group.cluster.id
  description       = "All traffic between cluster nodes"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  cidr_blocks       = [var.vpc_cidr]
}

resource "aws_security_group_rule" "cluster_egress" {
  type              = "egress"
  security_group_id = aws_security_group.cluster.id
  description       = "Allow cluster nodes to reach required external services"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  cidr_blocks       = ["0.0.0.0/0"]
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

  # IAM role names are global to the account, not per-region — so qualify
  # with the region to avoid collisions if multiple workspaces share the same
  # cluster_name. (The control plane is a single EC2 instance, so it can't
  # be multi-region anyway.)

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


resource "aws_iam_role_policy" "cp_ssm_write" {
  name = "${var.cluster_name}-cp-ssm-write"
  role = aws_iam_role.control_plane.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
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

  # Do not replace a running control plane merely because the latest Ubuntu
  # AMI changed. A fresh cluster still uses the current AMI automatically.
  lifecycle {
    ignore_changes = [ami]
  }

  tags = { Name = "${var.cluster_name}-control-plane" }
}

# These volumes already exist in Terraform state and are retained for the
# Prometheus persistence setup. Keeping the declarations prevents a normal
# cluster apply from deleting them while the Kubernetes PVC ownership is
# reconciled separately.
resource "aws_ebs_volume" "prometheus_dev" {
  availability_zone = var.prometheus_volume_availability_zone
  size              = 5
  type              = "gp3"

  tags = {
    Name        = "prometheus-data-dev"
    Environment = "dev"
    Project     = "PolyAI"
  }
}

resource "aws_ebs_volume" "prometheus_prod" {
  availability_zone = var.prometheus_volume_availability_zone
  size              = 5
  type              = "gp3"

  tags = {
    Name        = "prometheus-data-prod"
    Environment = "prod"
    Project     = "PolyAI"
  }
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
# ---------------------------------------------------------------------------
resource "aws_autoscaling_group" "worker" {
  name                = "${var.cluster_name}-worker-asg"
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

  # Workers must never launch before the control plane exists 
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
resource "aws_s3_bucket_versioning" "images" {
  bucket = aws_s3_bucket.images.id
  versioning_configuration {
    status = "Enabled"
  }
}
