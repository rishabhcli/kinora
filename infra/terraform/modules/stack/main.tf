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

  # OSS exposes an S3-compatible endpoint; the app's boto3 ObjectStore targets it.
  oss_s3_endpoint = "https://oss-${var.region}.aliyuncs.com"
}

# Latest Aliyun Linux 3 x64 system image (resolved at plan time, so no stale id).
data "alicloud_images" "app" {
  owners      = "system"
  name_regex  = "^aliyun_3_.*_x64.*"
  most_recent = true
}

module "secrets" {
  source = "../secrets"

  db_password    = var.db_account_password
  redis_password = var.redis_password
  jwt_secret     = var.jwt_secret
  mcp_auth_token = var.mcp_auth_token
}

module "network" {
  source = "../network"

  name          = local.name
  tags          = local.common_tags
  vpc_cidr      = var.vpc_cidr
  zones         = var.zones
  vswitch_cidrs = var.vswitch_cidrs
  admin_cidr    = var.admin_cidr
  ssh_cidr      = var.ssh_cidr
  enable_nat    = var.enable_nat
}

module "storage" {
  source = "../storage"

  bucket_name                = var.oss_bucket_name
  tags                       = local.common_tags
  versioning                 = true
  ia_transition_days         = var.oss_ia_transition_days
  noncurrent_expiration_days = var.oss_noncurrent_expiration_days
}

module "database" {
  source = "../database"

  name                  = local.name
  tags                  = local.common_tags
  engine_version        = var.rds_engine_version
  instance_type         = var.rds_instance_type
  instance_storage      = var.rds_instance_storage
  storage_type          = var.rds_storage_type
  backup_retention_days = var.rds_backup_retention_days
  high_availability     = var.rds_high_availability
  vswitch_id            = module.network.vswitch_ids[0]
  zone_id               = var.zones[0]
  vpc_cidr              = module.network.vpc_cidr
  db_name               = var.db_name
  account_name          = var.db_account_name
  account_password      = module.secrets.db_password
}

module "redis" {
  source = "../redis"

  name           = local.name
  tags           = local.common_tags
  instance_class = var.redis_instance_class
  engine_version = var.redis_engine_version
  vswitch_id     = module.network.vswitch_ids[0]
  zone_id        = var.zones[0]
  vpc_cidr       = module.network.vpc_cidr
  password       = module.secrets.redis_password
}

locals {
  database_url = format(
    "postgresql+asyncpg://%s:%s@%s:%s/%s",
    var.db_account_name,
    module.secrets.db_password,
    module.database.connection_string,
    module.database.port,
    var.db_name,
  )

  redis_url = format(
    "redis://:%s@%s:%s/0",
    module.secrets.redis_password,
    module.redis.connection_domain,
    module.redis.port,
  )
}

module "compute" {
  source = "../compute"

  name                     = local.name
  tags                     = local.common_tags
  image_id                 = data.alicloud_images.app.images[0].id
  instance_type            = var.ecs_instance_type
  security_group_id        = module.network.app_security_group_id
  vswitch_ids              = module.network.vswitch_ids
  system_disk_category     = var.ecs_system_disk_category
  ecs_password             = var.ecs_password
  internet_bandwidth_out   = var.ecs_internet_bandwidth_out
  render_worker_count      = var.render_worker_count
  ingest_worker_count      = var.ingest_worker_count
  container_image          = var.container_image
  frontend_container_image = var.frontend_container_image

  app_env            = var.environment
  database_url       = local.database_url
  redis_url          = local.redis_url
  s3_endpoint_url    = local.oss_s3_endpoint
  s3_region          = var.region
  s3_bucket          = module.storage.bucket
  s3_access_key      = var.alicloud_access_key
  s3_secret_key      = var.alicloud_secret_key
  dashscope_api_key  = var.dashscope_api_key
  dashscope_base_url = var.dashscope_base_url
  kinora_live_video  = var.kinora_live_video
  video_model        = var.video_model
  video_model_i2v    = var.video_model_i2v
  video_model_r2v    = var.video_model_r2v
  jwt_secret         = module.secrets.jwt_secret
  mcp_auth_token     = module.secrets.mcp_auth_token
  cors_origins       = var.cors_origins
}

module "observability" {
  source = "../observability"

  name               = local.name
  tags               = local.common_tags
  enabled            = var.observability_enabled
  log_retention_days = var.log_retention_days
}
