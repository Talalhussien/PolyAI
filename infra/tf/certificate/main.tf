terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # This state is deliberately separate from the cluster state. The cluster
  # may be destroyed and rebuilt without deleting the long-lived certificate.
  backend "s3" {
    bucket       = "talalhuss-polyai-tfstate"
    key          = "certificate.tfstate"
    region       = "us-east-1"
    use_lockfile = true
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_route53_zone" "shared" {
  name         = var.route53_zone_name
  private_zone = false
}

resource "aws_acm_certificate" "talalhuss" {
  domain_name               = "*.${var.certificate_subdomain}.${var.route53_zone_name}"
  subject_alternative_names = ["${var.certificate_subdomain}.${var.route53_zone_name}"]
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Name    = var.certificate_subdomain
    Project = "PolyAI"
  }
}

resource "aws_route53_record" "validation" {
  # ACM uses one DNS validation CNAME for the wildcard and its apex SAN.
  # The key is static so Terraform can plan before ACM returns the CNAME.
  zone_id = data.aws_route53_zone.shared.zone_id
  name = one([
    for option in aws_acm_certificate.talalhuss.domain_validation_options :
    option.resource_record_name
    if option.domain_name == aws_acm_certificate.talalhuss.domain_name
  ])
  type = one([
    for option in aws_acm_certificate.talalhuss.domain_validation_options :
    option.resource_record_type
    if option.domain_name == aws_acm_certificate.talalhuss.domain_name
  ])
  ttl = 60
  records = [one([
    for option in aws_acm_certificate.talalhuss.domain_validation_options :
    option.resource_record_value
    if option.domain_name == aws_acm_certificate.talalhuss.domain_name
  ])]
  allow_overwrite = false
}

resource "aws_acm_certificate_validation" "talalhuss" {
  certificate_arn = aws_acm_certificate.talalhuss.arn

  validation_record_fqdns = [aws_route53_record.validation.fqdn]
}
