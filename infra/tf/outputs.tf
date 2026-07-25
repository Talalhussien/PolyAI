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
