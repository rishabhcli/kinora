# ApsaraDB RDS for PostgreSQL — the canon graph, versioned continuity states, the
# episodic pgvector store, sessions, render jobs, prefs, and the budget ledger.
#
# pgvector: PostgreSQL >= 14 on RDS supports the `vector` extension. The app's
# initial Alembic migration runs `CREATE EXTENSION IF NOT EXISTS vector`, so the
# extension is enabled when `alembic upgrade head` first runs against this instance.

resource "alicloud_db_instance" "postgres" {
  engine                   = "PostgreSQL"
  engine_version           = var.rds_engine_version
  instance_type            = var.rds_instance_type
  instance_storage         = var.rds_instance_storage
  db_instance_storage_type = var.rds_storage_type
  instance_name            = "${local.name}-pg"

  vswitch_id = alicloud_vswitch.this[0].id
  zone_id    = var.zones[0]

  # Reachable only from inside the VPC (the app tier connects over the vswitch).
  security_ips = [var.vpc_cidr]

  tags = local.common_tags
}

resource "alicloud_db_database" "kinora" {
  instance_id    = alicloud_db_instance.postgres.id
  data_base_name = var.db_name
  character_set  = "UTF8"
  description    = "Kinora application database"
}

resource "alicloud_db_account" "kinora" {
  db_instance_id      = alicloud_db_instance.postgres.id
  account_name        = var.db_account_name
  account_password    = local.db_password
  account_type        = "Super"
  account_description = "Kinora application account"
}
