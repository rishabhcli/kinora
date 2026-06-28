# ---------------------------------------------------------------------------- #
# Kinora — STAGING environment (Alibaba Cloud)
#
# Two zones, mid-sized classes. Production-shaped but cheaper; a safe place to
# rehearse a deploy + run the §13 eval harness before prod. Live video stays OFF.
# ---------------------------------------------------------------------------- #

provider "alicloud" {
  access_key = var.alicloud_access_key != "" ? var.alicloud_access_key : null
  secret_key = var.alicloud_secret_key != "" ? var.alicloud_secret_key : null
  region     = var.region
}

module "kinora" {
  source = "../../modules/stack"

  project     = "kinora"
  environment = "staging"
  region      = var.region
  tags        = var.tags

  zones         = var.zones
  vpc_cidr      = "10.40.0.0/16"
  vswitch_cidrs = ["10.40.1.0/24", "10.40.2.0/24"]
  admin_cidr    = var.admin_cidr
  ssh_cidr      = var.ssh_cidr
  enable_nat    = true

  oss_bucket_name = var.oss_bucket_name

  rds_instance_type         = "pg.n2.medium.1"
  rds_instance_storage      = 50
  rds_backup_retention_days = 7
  rds_high_availability     = false

  redis_instance_class = "redis.master.small.default"

  ecs_instance_type          = "ecs.g7.large"
  ecs_password               = var.ecs_password
  ecs_internet_bandwidth_out = 10
  render_worker_count        = 1
  ingest_worker_count        = 1

  container_image          = var.container_image
  frontend_container_image = var.frontend_container_image
  dashscope_api_key        = var.dashscope_api_key
  kinora_live_video        = false

  alicloud_access_key = var.alicloud_access_key
  alicloud_secret_key = var.alicloud_secret_key

  jwt_secret     = var.jwt_secret
  mcp_auth_token = var.mcp_auth_token
  cors_origins   = var.cors_origins

  observability_enabled = true
  log_retention_days    = 14
}
