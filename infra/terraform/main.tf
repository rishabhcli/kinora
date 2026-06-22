# ---------------------------------------------------------------------------- #
# Locals + shared lookups
# ---------------------------------------------------------------------------- #

locals {
  name = "${var.project}-${var.environment}"

  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Application = "kinora"
    },
    var.tags,
  )

  # Resolve the DB / Redis passwords: use the provided value or a generated one.
  db_password    = var.db_account_password != "" ? var.db_account_password : random_password.db.result
  redis_password = var.redis_password != "" ? var.redis_password : random_password.redis.result
}

# URL-safe charset (letters/digits + - _) so the generated secrets drop cleanly
# into the DATABASE_URL / REDIS_URL DSNs without percent-encoding.
resource "random_password" "db" {
  length           = 24
  special          = true
  override_special = "-_"
}

resource "random_password" "redis" {
  length           = 24
  special          = true
  override_special = "-_"
}

# Latest Aliyun Linux 3 x64 system image (resolved at plan time, so no stale id).
data "alicloud_images" "app" {
  owners      = "system"
  name_regex  = "^aliyun_3_.*_x64.*"
  most_recent = true
}

# ---------------------------------------------------------------------------- #
# Network — VPC, per-zone vswitches
# ---------------------------------------------------------------------------- #

resource "alicloud_vpc" "this" {
  vpc_name   = "${local.name}-vpc"
  cidr_block = var.vpc_cidr
  tags       = local.common_tags
}

resource "alicloud_vswitch" "this" {
  count        = length(var.zones)
  vpc_id       = alicloud_vpc.this.id
  cidr_block   = var.vswitch_cidrs[count.index]
  zone_id      = var.zones[count.index]
  vswitch_name = "${local.name}-vsw-${count.index}"
  tags         = local.common_tags
}

# ---------------------------------------------------------------------------- #
# Security groups — app tier (public ingress) + data tier (intra-VPC only)
# ---------------------------------------------------------------------------- #

resource "alicloud_security_group" "app" {
  security_group_name = "${local.name}-app-sg"
  description         = "Kinora app tier (api/render-worker/mcp)"
  vpc_id              = alicloud_vpc.this.id
  tags                = local.common_tags
}

resource "alicloud_security_group" "data" {
  security_group_name = "${local.name}-data-sg"
  description         = "Kinora data tier (rds/tair) — intra-VPC only"
  vpc_id              = alicloud_vpc.this.id
  tags                = local.common_tags
}

# -- App tier ingress (lock var.admin_cidr down in production) --------------- #

resource "alicloud_security_group_rule" "app_api" {
  type              = "ingress"
  ip_protocol       = "tcp"
  port_range        = "8000/8000"
  security_group_id = alicloud_security_group.app.id
  cidr_ip           = var.admin_cidr
  nic_type          = "intranet"
  policy            = "accept"
  priority          = 1
}

resource "alicloud_security_group_rule" "app_mcp" {
  type              = "ingress"
  ip_protocol       = "tcp"
  port_range        = "8765/8765"
  security_group_id = alicloud_security_group.app.id
  cidr_ip           = var.admin_cidr
  nic_type          = "intranet"
  policy            = "accept"
  priority          = 1
}

resource "alicloud_security_group_rule" "app_ssh" {
  type              = "ingress"
  ip_protocol       = "tcp"
  port_range        = "22/22"
  security_group_id = alicloud_security_group.app.id
  cidr_ip           = var.admin_cidr
  nic_type          = "intranet"
  policy            = "accept"
  priority          = 1
}

# -- Data tier ingress: only from the app security group --------------------- #

resource "alicloud_security_group_rule" "data_postgres" {
  type                     = "ingress"
  ip_protocol              = "tcp"
  port_range               = "5432/5432"
  security_group_id        = alicloud_security_group.data.id
  source_security_group_id = alicloud_security_group.app.id
  nic_type                 = "intranet"
  policy                   = "accept"
  priority                 = 1
}

resource "alicloud_security_group_rule" "data_redis" {
  type                     = "ingress"
  ip_protocol              = "tcp"
  port_range               = "6379/6379"
  security_group_id        = alicloud_security_group.data.id
  source_security_group_id = alicloud_security_group.app.id
  nic_type                 = "intranet"
  policy                   = "accept"
  priority                 = 1
}
