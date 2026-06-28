# ---------------------------------------------------------------------------- #
# VPC + per-zone vswitches
# ---------------------------------------------------------------------------- #

resource "alicloud_vpc" "this" {
  vpc_name   = "${var.name}-vpc"
  cidr_block = var.vpc_cidr
  tags       = var.tags
}

resource "alicloud_vswitch" "this" {
  count        = length(var.zones)
  vpc_id       = alicloud_vpc.this.id
  cidr_block   = var.vswitch_cidrs[count.index]
  zone_id      = var.zones[count.index]
  vswitch_name = "${var.name}-vsw-${count.index}"
  tags         = var.tags
}

# ---------------------------------------------------------------------------- #
# Optional NAT gateway — lets private app nodes egress to DashScope + registries
# without a public IP. Only created when enable_nat = true.
# ---------------------------------------------------------------------------- #

resource "alicloud_nat_gateway" "this" {
  count            = var.enable_nat ? 1 : 0
  vpc_id           = alicloud_vpc.this.id
  vswitch_id       = alicloud_vswitch.this[0].id
  nat_gateway_name = "${var.name}-nat"
  nat_type         = "Enhanced"
  tags             = var.tags
}

resource "alicloud_eip_address" "nat" {
  count                = var.enable_nat ? 1 : 0
  address_name         = "${var.name}-nat-eip"
  bandwidth            = "100"
  internet_charge_type = "PayByTraffic"
  tags                 = var.tags
}

resource "alicloud_eip_association" "nat" {
  count         = var.enable_nat ? 1 : 0
  allocation_id = alicloud_eip_address.nat[0].id
  instance_id   = alicloud_nat_gateway.this[0].id
}

# A default SNAT entry per vswitch so every subnet egresses through the NAT EIP.
resource "alicloud_snat_entry" "this" {
  count             = var.enable_nat ? length(var.zones) : 0
  snat_table_id     = alicloud_nat_gateway.this[0].snat_table_ids
  source_vswitch_id = alicloud_vswitch.this[count.index].id
  snat_ip           = alicloud_eip_address.nat[0].ip_address
}

# ---------------------------------------------------------------------------- #
# Security groups — app tier (controlled public ingress) + data tier (intra-VPC)
# ---------------------------------------------------------------------------- #

resource "alicloud_security_group" "app" {
  security_group_name = "${var.name}-app-sg"
  description         = "Kinora app tier (api/frontend/render-worker/ingest-worker/mcp)"
  vpc_id              = alicloud_vpc.this.id
  tags                = var.tags
}

resource "alicloud_security_group" "data" {
  security_group_name = "${var.name}-data-sg"
  description         = "Kinora data tier (rds/tair) — intra-VPC only"
  vpc_id              = alicloud_vpc.this.id
  tags                = var.tags
}

# -- App tier ingress: three distinct fail-closed rules + intra-VPC MCP -------- #

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

resource "alicloud_security_group_rule" "app_frontend" {
  type              = "ingress"
  ip_protocol       = "tcp"
  port_range        = "80/80"
  security_group_id = alicloud_security_group.app.id
  cidr_ip           = var.admin_cidr
  nic_type          = "intranet"
  policy            = "accept"
  priority          = 1
}

# MCP (8765) is reachable only from other app-tier nodes — never the internet.
resource "alicloud_security_group_rule" "app_mcp" {
  type                     = "ingress"
  ip_protocol              = "tcp"
  port_range               = "8765/8765"
  security_group_id        = alicloud_security_group.app.id
  source_security_group_id = alicloud_security_group.app.id
  nic_type                 = "intranet"
  policy                   = "accept"
  priority                 = 1
}

resource "alicloud_security_group_rule" "app_ssh" {
  type              = "ingress"
  ip_protocol       = "tcp"
  port_range        = "22/22"
  security_group_id = alicloud_security_group.app.id
  cidr_ip           = var.ssh_cidr
  nic_type          = "intranet"
  policy            = "accept"
  priority          = 1
}

# -- Data tier ingress: only from the app security group ----------------------- #

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
