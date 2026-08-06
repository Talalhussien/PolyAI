output "alb_arn" {
  value = aws_lb.ingress.arn
}

output "alb_dns_name" {
  value = aws_lb.ingress.dns_name
}

output "alb_zone_id" {
  value = aws_lb.ingress.zone_id
}

output "target_group_arn" {
  value = aws_lb_target_group.ingress.arn
}

output "dns_records" {
  value = {
    for key, record in aws_route53_record.alias : key => record.fqdn
  }
}
