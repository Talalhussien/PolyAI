variable "cluster_name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "worker_asg_name" {
  type = string
}

variable "worker_security_group_id" {
  type = string
}

variable "route53_zone_name" {
  type = string
}

variable "acm_certificate_arn" {
  type     = string
  default  = null
  nullable = true
}

variable "acm_certificate_domain" {
  type = string
}

variable "http_node_port" {
  type = number
}

variable "https_node_port" {
  type = number
}

variable "dns_records" {
  type = map(string)
}
