variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "azs" {
  description = "Availability Zones to spread the public subnets across"
  type        = list(string)
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
