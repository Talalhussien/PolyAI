output "control_plane_public_ip" {
  description = "Public IP of the control plane instance"
  value       = aws_instance.control_plane.public_ip
}

output "control_plane_id" {
  description = "EC2 instance ID of the control plane"
  value       = aws_instance.control_plane.id
}

output "worker_asg_name" {
  description = "Name of the worker Auto Scaling Group"
  value       = aws_autoscaling_group.worker.name
}

output "security_group_id" {
  description = "Security group ID shared by control plane and workers"
  value       = aws_security_group.cluster.id
}

output "images_bucket_name" {
  description = "Name of the S3 bucket for application images"
  value       = aws_s3_bucket.images.bucket
}

output "images_bucket_arn" {
  description = "ARN of the S3 bucket for application images"
  value       = aws_s3_bucket.images.arn
}

output "prometheus_dev_volume_id" {
  description = "EBS volume ID for dev Prometheus storage — use as volumeHandle in infra/k8s/dev/pv/prometheus-pv.yaml"
  value       = aws_ebs_volume.prometheus_dev.id
}

output "prometheus_prod_volume_id" {
  description = "EBS volume ID for prod Prometheus storage — use as volumeHandle in infra/k8s/prod/pv/prometheus-pv.yaml"
  value       = aws_ebs_volume.prometheus_prod.id
}
