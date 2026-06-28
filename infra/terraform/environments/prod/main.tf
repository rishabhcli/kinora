# ---------------------------------------------------------------------------- #
# Kinora — PROD environment (Alibaba Cloud)
#
# Two zones, production classes, HA Postgres, >=2 render-workers, longer backups,
# centralised logging. Live video stays OFF by default — flip kinora_live_video
# deliberately (kinora.md §11.1). Validate-only here; apply needs your creds.
# ---------------------------------------------------------------------------- #

provider "alicloud" {
  access_key = var.alicloud_access_key != "" ? var.alicloud_access_key : null
  secret_key = var.alicloud_secret_key != "" ? var.alicloud_secret_key : null
  region     = var.region
}

module "kinora" {
  source = "../../modules/stack"

  project     = "kinora"
  environment = "prod"
  region      = var.region
  tags        = var.tags

  zones         = var.zones
  vpc_cidr      = "10.20.0.0/16"
  vswitch_cidrs = ["10.20.1.0/24", "10.20.2.0/24"]
  admin_cidr    = var.admin_cidr
  ssh_cidr      = var.ssh_cidr
  enable_nat    = true

  oss_bucket_name                = var.oss_bucket_name
  oss_ia_transition_days         = 30
  oss_noncurrent_expiration_days = 180

  rds_instance_type         = "pg.n4.medium.1"
  rds_instance_storage      = 100
  rds_backup_retention_days = 30
  rds_high_availability     = true

  redis_instance_class = "redis.master.small.default"

  ecs_instance_type          = "ecs.g7.xlarge"
  ecs_password               = var.ecs_password
  ecs_internet_bandwidth_out = 20
  render_worker_count        = 2
  ingest_worker_count        = 1

  container_image          = var.container_image
  frontend_container_image = var.frontend_container_image
  dashscope_api_key        = var.dashscope_api_key
  kinora_live_video        = false # deliberate opt-in only

  alicloud_access_key = var.alicloud_access_key
  alicloud_secret_key = var.alicloud_secret_key

  jwt_secret     = var.jwt_secret
  mcp_auth_token = var.mcp_auth_token
  cors_origins   = var.cors_origins

  observability_enabled = true
  log_retention_days    = 30
}
