variable "aws_region" {
  type        = string
  description = "AWS region where the ALB and ACM certificate are deployed"
  default     = "us-east-1"
}

variable "route53_zone_name" {
  type        = string
  description = "Existing public Route 53 hosted zone; it is looked up, not managed"
  default     = "fursa.click"
}

variable "certificate_subdomain" {
  type        = string
  description = "Personal DNS namespace under the shared hosted zone"
  default     = "talalhuss"
}
