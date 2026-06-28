# ---------------------------------------------------------------------------- #
# aws-security module — the security groups, mirroring the Alibaba posture:
#   * alb       : public 80/443 from admin_cidr only (no 0.0.0.0/0)
#   * app       : 8000 from the ALB SG; 8765 (MCP) only from the app SG itself
#                 (never internet-facing); all egress allowed (DashScope/OSS)
#   * data      : 5432 + 6379 only from the app SG (intra-VPC data tier)
# ---------------------------------------------------------------------------- #

variable "name" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "vpc_id" {
  type = string
}

variable "admin_cidr" {
  description = "CIDR allowed to reach the ALB (80/443). Never 0.0.0.0/0."
  type        = string

  validation {
    condition     = var.admin_cidr != "0.0.0.0/0"
    error_message = "admin_cidr must not be 0.0.0.0/0."
  }
}

# -- ALB SG: public entrypoint, locked to admin_cidr --------------------------- #
resource "aws_security_group" "alb" {
  name        = "${var.name}-alb-sg"
  description = "Kinora ALB — public 80/443 from admin_cidr"
  vpc_id      = var.vpc_id
  tags        = merge(var.tags, { Name = "${var.name}-alb-sg" })
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = var.admin_cidr
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = var.admin_cidr
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "alb_all" {
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# -- App SG: api/frontend behind the ALB; MCP intra-SG only -------------------- #
resource "aws_security_group" "app" {
  name        = "${var.name}-app-sg"
  description = "Kinora app tier (api/frontend/workers/mcp)"
  vpc_id      = var.vpc_id
  tags        = merge(var.tags, { Name = "${var.name}-app-sg" })
}

resource "aws_vpc_security_group_ingress_rule" "app_api_from_alb" {
  security_group_id            = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

resource "aws_vpc_security_group_ingress_rule" "app_frontend_from_alb" {
  security_group_id            = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 80
  to_port                      = 80
}

# MCP (8765): reachable only from other app-tier tasks — NEVER the ALB/internet.
resource "aws_vpc_security_group_ingress_rule" "app_mcp_intra" {
  security_group_id            = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 8765
  to_port                      = 8765
}

resource "aws_vpc_security_group_egress_rule" "app_all" {
  security_group_id = aws_security_group.app.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# -- Data SG: Postgres + Redis, only from the app SG --------------------------- #
resource "aws_security_group" "data" {
  name        = "${var.name}-data-sg"
  description = "Kinora data tier (rds/elasticache) — intra-VPC only"
  vpc_id      = var.vpc_id
  tags        = merge(var.tags, { Name = "${var.name}-data-sg" })
}

resource "aws_vpc_security_group_ingress_rule" "data_postgres" {
  security_group_id            = aws_security_group.data.id
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "data_redis" {
  security_group_id            = aws_security_group.data.id
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}

output "alb_sg_id" {
  value = aws_security_group.alb.id
}

output "app_sg_id" {
  value = aws_security_group.app.id
}

output "data_sg_id" {
  value = aws_security_group.data.id
}
