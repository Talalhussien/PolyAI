variable "cluster_name" {
  description = "Name prefix used to tag/name all resources in this module"
  type        = string
}

variable "aws_region" {
  description = "AWS region (needed inside user data scripts for aws CLI calls)"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID to launch instances into"
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR of the VPC, used to scope the intra-VPC security group rule"
  type        = string
}

variable "public_subnet_ids" {
  description = "Public subnet IDs the control plane and workers may launch into"
  type        = list(string)
}

variable "key_pair_name" {
  description = "Existing EC2 key pair name used for SSH access"
  type        = string
}

variable "allowed_ssh_cidr" {
  description = "CIDR allowed to SSH into instances"
  type        = string
}

variable "control_plane_instance_type" {
  type = string
}

variable "worker_instance_type" {
  type = string
}

variable "worker_min_size" {
  type = number
}

variable "worker_max_size" {
  type = number
}

variable "worker_desired_capacity" {
  type = number
}

variable "kubernetes_version" {
  description = "Kubernetes minor version to install, e.g. 1.30"
  type        = string
}

variable "s3_bucket_name" {
  description = "Name of the S3 bucket the app services read/write images to"
  type        = string
}
