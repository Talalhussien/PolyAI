variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "public_subnet_cidrs" {
  description = "CIDR blocks for the public subnets (one per AZ)"
  type        = list(string)
}

variable "cluster_name" {
  description = "Name prefix used to tag all cluster resources"
  type        = string
  default     = "talalhuss"
}

variable "key_pair_name" {
  description = "Existing EC2 key pair name used for SSH access"
  type        = string
}

variable "allowed_ssh_cidr" {
  description = "CIDR allowed to SSH into instances (use your own IP/32, not 0.0.0.0/0)"
  type        = string
}

variable "control_plane_instance_type" {
  description = "Instance type for the Kubernetes control plane"
  type        = string
  default     = "t3.medium"
}

variable "worker_instance_type" {
  description = "Instance type for Kubernetes worker nodes"
  type        = string
  default     = "t3.medium"
}

variable "worker_min_size" {
  description = "Minimum number of worker nodes in the ASG"
  type        = number
  default     = 1
}

variable "worker_max_size" {
  description = "Maximum number of worker nodes in the ASG"
  type        = number
  default     = 3
}

variable "worker_desired_capacity" {
  description = "Desired number of worker nodes right now. Must be >= worker_min_size (AWS constraint) — to idle at 0, set worker_min_size to 0 too."
  type        = number
  default     = 1
}

variable "kubernetes_version" {
  description = "Kubernetes minor version to install, e.g. 1.30"
  type        = string
  default     = "1.30"
}

variable "s3_bucket_name" {
  description = "Name of the S3 bucket the app services (YOLO/Agent/img-proc-mcp) read/write images to"
  type        = string
}

variable "route53_zone_name" {
  description = "Existing public Route 53 hosted zone used for platform DNS records"
  type        = string
  default     = "fursa.click"
}

variable "acm_certificate_arn" {
  description = "Optional existing ACM certificate ARN. When null, Terraform looks up an ISSUED certificate by acm_certificate_domain."
  type        = string
  default     = null
  nullable    = true
}

variable "acm_certificate_domain" {
  description = "Domain or wildcard name used to discover the existing ACM certificate"
  type        = string
  default     = "*.fursa.click"
}

variable "ingress_http_node_port" {
  description = "Fixed HTTP NodePort exposed by ingress-nginx"
  type        = number
  default     = 30080

  validation {
    condition     = var.ingress_http_node_port >= 30000 && var.ingress_http_node_port <= 32767
    error_message = "The HTTP ingress NodePort must be in the Kubernetes NodePort range."
  }
}

variable "ingress_https_node_port" {
  description = "Fixed HTTPS NodePort exposed by ingress-nginx"
  type        = number
  default     = 30443

  validation {
    condition     = var.ingress_https_node_port >= 30000 && var.ingress_https_node_port <= 32767
    error_message = "The HTTPS ingress NodePort must be in the Kubernetes NodePort range."
  }
}

variable "dns_records" {
  description = "Map of stable record keys to hostnames that alias the shared ALB"
  type        = map(string)
  default = {
    dev_frontend  = "dev.fursa.click"
    dev_agent     = "dev-agent.fursa.click"
    prod_frontend = "app.fursa.click"
    prod_agent    = "prod-agent.fursa.click"
    grafana       = "grafana.fursa.click"
    prometheus    = "prometheus.fursa.click"
    argocd        = "argocd.fursa.click"
  }
}
