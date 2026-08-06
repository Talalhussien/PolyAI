output "certificate_arn" {
  description = "Issued ACM certificate ARN used by the cluster ALB"
  value       = aws_acm_certificate_validation.talalhuss.certificate_arn
}

output "certificate_domain" {
  description = "Wildcard certificate domain"
  value       = aws_acm_certificate.talalhuss.domain_name
}

output "certificate_status" {
  description = "ACM certificate status after validation"
  value       = aws_acm_certificate.talalhuss.status
}
