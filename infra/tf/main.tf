terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  #S3 backend gives GitHub Actions and your laptop the same locked,versioned, 
  #recoverable state file, auto-separated per region by workspace, all in one bucket.
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


#AZs are fetched dynamically and sorted alphabetically for a stable order
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

module "ingress" {
  source = "./modules/ingress"

  cluster_name             = var.cluster_name
  vpc_id                   = module.vpc.vpc_id
  public_subnet_ids        = module.vpc.public_subnets
  worker_asg_name          = module.k8s_cluster.worker_asg_name
  worker_security_group_id = module.k8s_cluster.security_group_id

  route53_zone_name      = var.route53_zone_name
  acm_certificate_arn    = var.acm_certificate_arn
  acm_certificate_domain = var.acm_certificate_domain
  http_node_port         = var.ingress_http_node_port
  https_node_port        = var.ingress_https_node_port
  dns_records            = var.dns_records
}
