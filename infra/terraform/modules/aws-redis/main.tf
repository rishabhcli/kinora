# ---------------------------------------------------------------------------- #
# aws-redis module — ElastiCache for Redis (the Tair analogue). The priority
# render queue, scheduler state, pub/sub, locks. In private subnets, reachable
# only from the data SG; AUTH token + in-transit + at-rest encryption.
# ---------------------------------------------------------------------------- #

variable "name" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "node_type" {
  type = string
}

variable "engine_version" {
  type    = string
  default = "7.1"
}

variable "subnet_ids" {
  type = list(string)
}

variable "security_group_id" {
  type = string
}

variable "auth_token" {
  description = "Redis AUTH token (>= 16 chars for ElastiCache)."
  type        = string
  sensitive   = true
}

variable "multi_az" {
  description = "Enable Multi-AZ with an automatic-failover replica."
  type        = bool
  default     = false
}

resource "aws_elasticache_subnet_group" "this" {
  name       = "${var.name}-redis-subnets"
  subnet_ids = var.subnet_ids
  tags       = var.tags
}

resource "aws_elasticache_replication_group" "this" {
  replication_group_id = "${var.name}-redis"
  description          = "Kinora render queue / scheduler state / pub-sub"
  engine               = "redis"
  engine_version       = var.engine_version
  node_type            = var.node_type
  port                 = 6379

  # One node for dev; a primary + replica with automatic failover for HA.
  num_cache_clusters         = var.multi_az ? 2 : 1
  automatic_failover_enabled = var.multi_az
  multi_az_enabled           = var.multi_az

  subnet_group_name  = aws_elasticache_subnet_group.this.name
  security_group_ids = [var.security_group_id]

  transit_encryption_enabled = true
  at_rest_encryption_enabled = true
  auth_token                 = var.auth_token

  tags = var.tags
}

output "primary_endpoint" {
  value = aws_elasticache_replication_group.this.primary_endpoint_address
}

output "port" {
  value = aws_elasticache_replication_group.this.port
}
