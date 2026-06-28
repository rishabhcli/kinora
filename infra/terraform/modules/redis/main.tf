# ---------------------------------------------------------------------------- #
# redis module — Tair (Redis OSS-compatible). The priority render queue, scheduler
# session state, pub/sub SSE fanout, dedup locks, and rate limiting. AUTH-protected
# and reachable only from inside the VPC.
# ---------------------------------------------------------------------------- #

variable "name" {
  description = "Resource name prefix."
  type        = string
}

variable "tags" {
  description = "Tags applied to the instance."
  type        = map(string)
  default     = {}
}

variable "instance_class" {
  description = "Tair/Redis instance class (region-specific)."
  type        = string
}

variable "engine_version" {
  description = "Redis engine version."
  type        = string
  default     = "7.0"
}

variable "vswitch_id" {
  description = "vswitch the instance is placed in."
  type        = string
}

variable "zone_id" {
  description = "Availability zone for the instance."
  type        = string
}

variable "vpc_cidr" {
  description = "VPC CIDR — the only IPs allowed to reach the instance."
  type        = string
}

variable "password" {
  description = "Resolved Redis AUTH password (from the secrets module)."
  type        = string
  sensitive   = true
}

resource "alicloud_kvstore_instance" "this" {
  db_instance_name = "${var.name}-redis"
  instance_class   = var.instance_class
  instance_type    = "Redis"
  engine_version   = var.engine_version

  vswitch_id = var.vswitch_id
  zone_id    = var.zone_id

  password     = var.password
  security_ips = [var.vpc_cidr]

  tags = var.tags
}

output "connection_domain" {
  description = "Tair/Redis internal endpoint host."
  value       = alicloud_kvstore_instance.this.connection_domain
}

output "port" {
  value = alicloud_kvstore_instance.this.port
}
