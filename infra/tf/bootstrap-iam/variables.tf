variable "aws_region" {
  description = "AWS region containing the ACM certificate"
  type        = string
  default     = "us-east-1"
}

variable "github_role_name" {
  description = "Existing IAM role assumed by the GitHub Actions workflow"
  type        = string
  default     = "talalhuss-github-terraform"
}

variable "route53_zone_id" {
  description = "Existing shared public Route 53 hosted zone ID"
  type        = string
  default     = "Z0068791177VM3T59WYDS"
}

variable "certificate_id" {
  description = "Existing issued ACM certificate UUID without the certificate ARN prefix"
  type        = string
  default     = "b7095805-0059-4cc0-a0d3-0f6c2279f636"
}
