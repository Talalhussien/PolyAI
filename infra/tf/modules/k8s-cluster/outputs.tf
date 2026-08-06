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
