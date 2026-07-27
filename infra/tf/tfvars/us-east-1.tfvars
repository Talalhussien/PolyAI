# Region-specific values for us-east-1.
# To provision in a different region: copy this file to tfvars/<region>.tfvars,
# update the values below, then `terraform workspace new <region>` and
# `terraform plan -var-file=tfvars/<region>.tfvars`.

aws_region = "us-east-1"

azs                 = ["us-east-1a", "us-east-1b"]
vpc_cidr            = "10.0.0.0/16"
public_subnet_cidrs = ["10.0.1.0/24", "10.0.2.0/24"]

cluster_name = "talalhuss"

# Existing EC2 key pair in this AWS account/region.
key_pair_name = "talal key1"

# My current public IP. Never leave this as 0.0.0.0/0.
allowed_ssh_cidr = "79.177.155.6/32"

control_plane_instance_type = "t3.medium"
worker_instance_type        = "t3.medium"

# AWS requires desired_capacity >= min_size, so min_size and
# desired_capacity must be changed together, not desired_capacity alone.
#
# While actively working on the cluster:
worker_min_size         = 1
worker_max_size         = 3
worker_desired_capacity = 1
#
# When finished, to stop paying for worker EC2 instances, switch to:
# worker_min_size         = 0
# worker_max_size         = 3
# worker_desired_capacity = 0

kubernetes_version = "1.30"

# Must be globally unique across all of AWS, not just this account.
s3_bucket_name = "talalhuss-polyai-images"
