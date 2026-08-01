terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Remote state — required so both GitHub Actions (ephemeral runners) and
  # local machines read/write the exact same state, with locking to prevent
  # concurrent applies and versioning for recovery. State is namespaced per
  # Terraform workspace automatically (env:/<workspace>/cluster.tfstate),
  # so one bucket serves every region without further config.
  backend "s3" {
    bucket       = "talalhuss-polyai-tfstate"
    key          = "cluster.tfstate"
    region       = "us-east-1"
    use_lockfile = true
  }
}

provider "aws" {
  region = var.aws_region
}

# Fetched dynamically instead of hardcoded, so this config isn't tied to
# assumptions about which AZs happen to be available in one specific AWS
# account. sort() makes the order deterministic (alphabetical) rather than
# whatever order the AWS API returns — azs[1] must stay us-east-1b, since
# the worker and Prometheus EBS volumes are pinned there and EBS volumes
# are AZ-locked.
data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(sort(data.aws_availability_zones.available.names), 0, 2)
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = "${var.cluster_name}-vpc"
  cidr = var.vpc_cidr

  azs            = local.azs
  public_subnets = var.public_subnet_cidrs

  # No private subnets / NAT gateway: every instance is public, which keeps
  # this lab cluster simple and avoids NAT Gateway hourly cost.
  enable_dns_hostnames = true
  enable_dns_support   = true

  map_public_ip_on_launch = true

  tags = {
    Project = var.cluster_name
  }
}

module "k8s_cluster" {
  source = "./modules/k8s-cluster"

  cluster_name      = var.cluster_name
  aws_region        = var.aws_region
  vpc_id            = module.vpc.vpc_id
  vpc_cidr          = var.vpc_cidr
  azs               = local.azs
  public_subnet_ids = module.vpc.public_subnets

  key_pair_name    = var.key_pair_name
  allowed_ssh_cidr = var.allowed_ssh_cidr

  control_plane_instance_type = var.control_plane_instance_type
  worker_instance_type        = var.worker_instance_type

  worker_min_size         = var.worker_min_size
  worker_max_size         = var.worker_max_size
  worker_desired_capacity = var.worker_desired_capacity

  kubernetes_version = var.kubernetes_version

  s3_bucket_name = var.s3_bucket_name
}
