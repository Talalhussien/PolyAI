# Region-specific values for us-east-1.
# To provision in a different region: copy this file to tfvars/<region>.tfvars,
# update the values below, then `terraform workspace new <region>` and
# `terraform plan -var-file=tfvars/<region>.tfvars`.

aws_region = "us-east-1"

vpc_cidr            = "10.0.0.0/16"
public_subnet_cidrs = ["10.0.1.0/24", "10.0.2.0/24"]

cluster_name = "talalhuss"

# Existing EC2 key pair in this AWS account/region.
key_pair_name = "talal key1"

# Open to the internet — my IP changes between sessions and this avoids
# re-editing this file every time. Real exposure risk: SSH is still
# key-only, but the port itself is reachable by anyone, not just me.
allowed_ssh_cidr = "0.0.0.0/0"

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
