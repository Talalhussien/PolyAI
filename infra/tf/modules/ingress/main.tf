data "aws_route53_zone" "shared" {
  name         = var.route53_zone_name
  private_zone = false
}

data "aws_acm_certificate" "existing" {
  count = var.acm_certificate_arn == null ? 1 : 0

  domain      = var.acm_certificate_domain
  statuses    = ["ISSUED"]
  most_recent = true
}

locals {
  certificate_arn = var.acm_certificate_arn != null ? var.acm_certificate_arn : data.aws_acm_certificate.existing[0].arn
}

resource "aws_security_group" "alb" {
  name        = "${var.cluster_name}-ingress-alb-sg"
  description = "Public ALB security group for the PolyAI ingress"
  vpc_id      = var.vpc_id

  tags = {
    Name    = "${var.cluster_name}-ingress-alb-sg"
    Project = var.cluster_name
  }
}

resource "aws_security_group_rule" "alb_http" {
  type              = "ingress"
  security_group_id = aws_security_group.alb.id
  description       = "HTTP for redirect to HTTPS"
  from_port         = 80
  to_port           = 80
  protocol          = "tcp"
  cidr_blocks       = ["0.0.0.0/0"]
}

resource "aws_security_group_rule" "alb_https" {
  type              = "ingress"
  security_group_id = aws_security_group.alb.id
  description       = "Public HTTPS application traffic"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  cidr_blocks       = ["0.0.0.0/0"]
}

resource "aws_security_group_rule" "alb_egress" {
  type              = "egress"
  security_group_id = aws_security_group.alb.id
  description       = "Allow ALB health checks and backend traffic"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  cidr_blocks       = ["0.0.0.0/0"]
}

resource "aws_security_group_rule" "worker_http_from_alb" {
  type                     = "ingress"
  security_group_id        = var.worker_security_group_id
  description              = "ALB to ingress-nginx HTTP NodePort"
  from_port                = var.http_node_port
  to_port                  = var.http_node_port
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.alb.id
}

resource "aws_security_group_rule" "worker_https_from_alb" {
  type                     = "ingress"
  security_group_id        = var.worker_security_group_id
  description              = "ALB to ingress-nginx HTTPS NodePort"
  from_port                = var.https_node_port
  to_port                  = var.https_node_port
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.alb.id
}

resource "aws_lb" "ingress" {
  name               = substr("${var.cluster_name}-ingress-alb", 0, 32)
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids

  tags = {
    Name    = "${var.cluster_name}-ingress-alb"
    Project = var.cluster_name
  }
}

resource "aws_lb_target_group" "ingress" {
  name                 = substr("${var.cluster_name}-ingress-tg", 0, 32)
  port                 = var.http_node_port
  protocol             = "HTTP"
  target_type          = "instance"
  vpc_id               = var.vpc_id
  deregistration_delay = 30

  health_check {
    enabled             = true
    protocol            = "HTTP"
    port                = "traffic-port"
    path                = "/"
    matcher             = "200-499"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = {
    Name    = "${var.cluster_name}-ingress-tg"
    Project = var.cluster_name
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.ingress.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.ingress.arn
  port              = 443
  protocol          = "HTTPS"
  certificate_arn   = local.certificate_arn
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.ingress.arn
  }
}

resource "aws_autoscaling_attachment" "worker" {
  autoscaling_group_name = var.worker_asg_name
  lb_target_group_arn    = aws_lb_target_group.ingress.arn
}

resource "aws_route53_record" "alias" {
  for_each = var.dns_records

  zone_id = data.aws_route53_zone.shared.zone_id
  name    = each.value
  type    = "A"

  alias {
    name                   = aws_lb.ingress.dns_name
    zone_id                = aws_lb.ingress.zone_id
    evaluate_target_health = true
  }
}
