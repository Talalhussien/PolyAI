output "vpc_id" {
  description = "ID of the VPC created for the cluster"
  value       = module.vpc.vpc_id
}

output "public_subnet_ids" {
  description = "IDs of the public subnets"
  value       = module.vpc.public_subnets
}

output "control_plane_public_ip" {
  description = "Public IP of the Kubernetes control plane (use to SSH and to run kubectl)"
  value       = module.k8s_cluster.control_plane_public_ip
}

output "worker_asg_name" {
  description = "Name of the worker Auto Scaling Group"
  value       = module.k8s_cluster.worker_asg_name
}

output "security_group_id" {
  description = "Security group shared by control plane and worker nodes"
  value       = module.k8s_cluster.security_group_id
}

output "images_bucket_name" {
  description = "Name of the S3 bucket for application images"
  value       = module.k8s_cluster.images_bucket_name
}

output "ingress_alb_dns_name" {
  description = "Public DNS name of the shared ingress Application Load Balancer"
  value       = module.ingress.alb_dns_name
}

output "ingress_alb_zone_id" {
  description = "Route 53 alias zone ID of the shared ingress Application Load Balancer"
  value       = module.ingress.alb_zone_id
}

output "ingress_dns_records" {
  description = "Fully-qualified DNS names managed for the shared ingress ALB"
  value       = module.ingress.dns_records
}
