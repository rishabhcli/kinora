# ---------------------------------------------------------------------------- #
# database module — ApsaraDB RDS for PostgreSQL. Holds the canon graph, versioned
# continuity states, the episodic pgvector store, sessions, render jobs, prefs,
# and the budget ledger. PostgreSQL >= 14 supports the `vector` extension, which
# the app's first Alembic migration enables (CREATE EXTENSION IF NOT EXISTS vector).
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

variable "engine_version" {
  description = "PostgreSQL major version (>= 14 for pgvector)."
  type        = string
  default     = "16.0"
}

variable "instance_type" {
  description = "RDS instance class (region-specific)."
  type        = string
}

variable "instance_storage" {
  description = "Data disk size in GB."
  type        = number
}

variable "storage_type" {
  description = "RDS storage type (cloud_essd | cloud_ssd)."
  type        = string
  default     = "cloud_essd"
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

variable "db_name" {
  description = "Application database name."
  type        = string
  default     = "kinora"
}

variable "account_name" {
  description = "Application database account."
  type        = string
  default     = "kinora"
}

variable "account_password" {
  description = "Resolved DB account password (from the secrets module)."
  type        = string
  sensitive   = true
}

variable "backup_retention_days" {
  description = "Automated backup retention in days (0 leaves the RDS default)."
  type        = number
  default     = 7
}

variable "high_availability" {
  description = "Use the HA (multi-AZ) category instead of basic single-node."
  type        = bool
  default     = false
}

resource "alicloud_db_instance" "this" {
  engine                   = "PostgreSQL"
  engine_version           = var.engine_version
  instance_type            = var.instance_type
  instance_storage         = var.instance_storage
  db_instance_storage_type = var.storage_type
  instance_name            = "${var.name}-pg"
  category                 = var.high_availability ? "HighAvailability" : "Basic"

  vswitch_id = var.vswitch_id
  zone_id    = var.zone_id

  # Reachable only from inside the VPC (the app tier connects over the vswitch).
  security_ips = [var.vpc_cidr]

  tags = var.tags
}

# Automated backup policy (kept conditional so dev can run the RDS default).
resource "alicloud_db_backup_policy" "this" {
  count                   = var.backup_retention_days > 0 ? 1 : 0
  instance_id             = alicloud_db_instance.this.id
  backup_retention_period = var.backup_retention_days
  preferred_backup_time   = "02:00Z-03:00Z"
  preferred_backup_period = ["Monday", "Wednesday", "Friday", "Sunday"]
  enable_backup_log       = true
}

resource "alicloud_db_database" "this" {
  instance_id    = alicloud_db_instance.this.id
  data_base_name = var.db_name
  character_set  = "UTF8"
  description    = "Kinora application database"
}

resource "alicloud_db_account" "this" {
  db_instance_id      = alicloud_db_instance.this.id
  account_name        = var.account_name
  account_password    = var.account_password
  account_type        = "Super"
  account_description = "Kinora application account"
}

output "instance_id" {
  value = alicloud_db_instance.this.id
}

output "connection_string" {
  description = "RDS PostgreSQL internal endpoint host."
  value       = alicloud_db_instance.this.connection_string
}

output "port" {
  value = alicloud_db_instance.this.port
}
