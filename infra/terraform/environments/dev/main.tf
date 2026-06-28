# ---------------------------------------------------------------------------- #
# Kinora — DEV environment (Alibaba Cloud)
#
# A thin root that composes the `stack` module with dev-sized inputs. The whole
# topology lives in modules/stack; this file only varies sizing/scale. Validate
# with `terraform init -backend=false && terraform validate` — never `apply`-ed
# in this repo (it needs your Alibaba credentials).
# ---------------------------------------------------------------------------- #

provider "alicloud" {
  access_key = var.alicloud_access_key != "" ? var.alicloud_access_key : null
  secret_key = var.alicloud_secret_key != "" ? var.alicloud_secret_key : null
  region     = var.region
}

module "kinora" {
  source = "../../modules/stack"

  project     = "kinora"
  environment = "dev"
  region      = var.region
  tags        = var.tags

  # Single zone is plenty for dev; smallest viable classes.
  zones         = [var.zones[0]]
  vpc_cidr      = "10.30.0.0/16"
  vswitch_cidrs = ["10.30.1.0/24"]
  admin_cidr    = var.admin_cidr
  ssh_cidr      = var.ssh_cidr
  enable_nat    = false

  oss_bucket_name = var.oss_bucket_name

  rds_instance_type         = "pg.n2.small.1"
  rds_instance_storage      = 20
  rds_backup_retention_days = 0 # use the RDS default in dev
  rds_high_availability     = false

  redis_instance_class = "redis.master.micro.default"

  ecs_instance_type          = "ecs.g7.large"
  ecs_password               = var.ecs_password
  ecs_internet_bandwidth_out = 5
  render_worker_count        = 1
  ingest_worker_count        = 1

  container_image          = var.container_image
  frontend_container_image = var.frontend_container_image
  dashscope_api_key        = var.dashscope_api_key
  kinora_live_video        = false # never default-on

  alicloud_access_key = var.alicloud_access_key
  alicloud_secret_key = var.alicloud_secret_key

  jwt_secret     = var.jwt_secret
  mcp_auth_token = var.mcp_auth_token
  cors_origins   = var.cors_origins

  observability_enabled = false # keep dev lean
  log_retention_days    = 7
}
