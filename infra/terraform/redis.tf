# Tair (Redis OSS-compatible) — the priority render queue, scheduler session
# state, pub/sub SSE fanout, dedup locks, and rate limiting.

resource "alicloud_kvstore_instance" "redis" {
  db_instance_name = "${local.name}-redis"
  instance_class   = var.redis_instance_class
  instance_type    = "Redis"
  engine_version   = var.redis_engine_version

  vswitch_id = alicloud_vswitch.this[0].id
  zone_id    = var.zones[0]

  # AUTH password; reachable only from inside the VPC.
  password     = local.redis_password
  security_ips = [var.vpc_cidr]

  tags = local.common_tags
}
