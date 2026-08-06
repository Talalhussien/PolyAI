terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Keep this state separate from the cluster. The GitHub Actions role must
  # remain available when the Kubernetes cluster is destroyed and rebuilt.
  backend "s3" {
    bucket       = "talalhuss-polyai-tfstate"
    key          = "github-terraform-permissions.tfstate"
    region       = "us-east-1"
    use_lockfile = true
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

locals {
  hosted_zone_arn = "arn:aws:route53:::hostedzone/${var.route53_zone_id}"
  certificate_arn = "arn:aws:acm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:certificate/${var.certificate_id}"
}

# This is intentionally a separate inline policy. It leaves the existing
# GitHub Terraform policy intact and adds only permissions needed by the
# certificate and ingress stacks.
resource "aws_iam_role_policy" "github_terraform_ingress" {
  name = "talalhuss-github-terraform-ingress"
  role = var.github_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Route53Discovery"
        Effect = "Allow"
        Action = [
          "route53:ListHostedZones",
          "route53:ListHostedZonesByName"
        ]
        Resource = "*"
      },
      {
        Sid    = "Route53RecordsInSharedZone"
        Effect = "Allow"
        Action = [
          "route53:ChangeResourceRecordSets",
          "route53:GetHostedZone",
          "route53:ListResourceRecordSets",
          "route53:ListTagsForResource"
        ]
        Resource = local.hosted_zone_arn
      },
      {
        Sid      = "Route53ChangeStatus"
        Effect   = "Allow"
        Action   = ["route53:GetChange"]
        Resource = "arn:aws:route53:::change/*"
      },
      {
        Sid    = "ReadIssuedIngressCertificate"
        Effect = "Allow"
        Action = [
          "acm:DescribeCertificate",
          "acm:GetCertificate",
          "acm:ListTagsForCertificate"
        ]
        Resource = local.certificate_arn
      },
      {
        Sid      = "DiscoverIssuedCertificates"
        Effect   = "Allow"
        Action   = ["acm:ListCertificates"]
        Resource = "*"
      },
      {
        Sid    = "IngressApplicationLoadBalancer"
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:AddTags",
          "elasticloadbalancing:CreateListener",
          "elasticloadbalancing:CreateLoadBalancer",
          "elasticloadbalancing:CreateTargetGroup",
          "elasticloadbalancing:DeleteListener",
          "elasticloadbalancing:DeleteLoadBalancer",
          "elasticloadbalancing:DeleteTargetGroup",
          "elasticloadbalancing:DeregisterTargets",
          "elasticloadbalancing:Describe*",
          "elasticloadbalancing:ModifyListener",
          "elasticloadbalancing:ModifyTargetGroup",
          "elasticloadbalancing:ModifyLoadBalancerAttributes",
          "elasticloadbalancing:RegisterTargets",
          "elasticloadbalancing:RemoveTags"
        ]
        Resource = "*"
      }
    ]
  })
}

output "github_role_name" {
  description = "GitHub Actions role receiving the ingress permissions"
  value       = var.github_role_name
}

output "policy_name" {
  description = "Additional inline policy managed by this stack"
  value       = aws_iam_role_policy.github_terraform_ingress.name
}
