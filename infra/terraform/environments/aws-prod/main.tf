# ---------------------------------------------------------------------------- #
# Kinora — AWS portable target (prod-shaped)
#
# A faithful AWS mirror of the Alibaba stack so the architecture isn't cloud-
# locked: VPC + subnets + SGs (fail-closed, MCP intra-VPC only), S3 (OSS
# analogue), RDS PostgreSQL (pgvector), ElastiCache Redis, Secrets Manager, and
# ECS-on-Fargate (one service per role, api+frontend behind an ALB, mcp internal).
# Same KINORA_LIVE_VIDEO=off default. Validate-only — never `apply`-ed here.
# ---------------------------------------------------------------------------- #

provider "aws" {
  region = var.region

  # Credentials come from the standard AWS chain (env / shared config / SSO /
  # instance role) — nothing secret is committed. default_tags stamp everything.
  default_tags {
    tags = local.common_tags
  }
}

locals {
  name = "kinora-${var.environment}"
  common_tags = merge({
    Project     = "kinora"
    Environment = var.environment
    ManagedBy   = "terraform"
    Application = "kinora"
  }, var.tags)
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

module "network" {
  source = "../../modules/aws-network"

  name                 = local.name
  tags                 = local.common_tags
  vpc_cidr             = "10.60.0.0/16"
  azs                  = local.azs
  public_subnet_cidrs  = ["10.60.0.0/24", "10.60.1.0/24"]
  private_subnet_cidrs = ["10.60.10.0/24", "10.60.11.0/24"]
  single_nat_gateway   = var.environment == "dev"
}

module "security" {
  source = "../../modules/aws-security"

  name       = local.name
  tags       = local.common_tags
  vpc_id     = module.network.vpc_id
  admin_cidr = var.admin_cidr
}

module "storage" {
  source = "../../modules/aws-storage"

  bucket_name   = var.s3_bucket_name
  tags          = local.common_tags
  force_destroy = var.environment == "dev"
}

module "database" {
  source = "../../modules/aws-database"

  name                  = local.name
  tags                  = local.common_tags
  instance_class        = var.rds_instance_class
  allocated_storage     = 100
  subnet_ids            = module.network.private_subnet_ids
  security_group_id     = module.security.data_sg_id
  password              = var.db_password
  multi_az              = var.environment == "prod"
  backup_retention_days = var.environment == "prod" ? 30 : 7
  deletion_protection   = var.environment == "prod"
}

module "redis" {
  source = "../../modules/aws-redis"

  name              = local.name
  tags              = local.common_tags
  node_type         = var.redis_node_type
  subnet_ids        = module.network.private_subnet_ids
  security_group_id = module.security.data_sg_id
  auth_token        = var.redis_auth_token
  multi_az          = var.environment == "prod"
}

locals {
  database_url = "postgresql+asyncpg://${var.db_username}:${var.db_password}@${module.database.address}:${module.database.port}/${var.db_name}"
  redis_url    = "rediss://:${var.redis_auth_token}@${module.redis.primary_endpoint}:${module.redis.port}/0"
}

module "secrets" {
  source = "../../modules/aws-secrets"

  name              = local.name
  tags              = local.common_tags
  database_url      = local.database_url
  redis_url         = local.redis_url
  dashscope_api_key = var.dashscope_api_key
  jwt_secret        = var.jwt_secret
  mcp_auth_token    = var.mcp_auth_token
  s3_bucket_arn     = module.storage.arn
}

module "compute" {
  source = "../../modules/aws-compute"

  name                     = local.name
  tags                     = local.common_tags
  region                   = var.region
  vpc_id                   = module.network.vpc_id
  public_subnet_ids        = module.network.public_subnet_ids
  private_subnet_ids       = module.network.private_subnet_ids
  alb_sg_id                = module.security.alb_sg_id
  app_sg_id                = module.security.app_sg_id
  container_image          = var.container_image
  frontend_container_image = var.frontend_container_image

  render_worker_desired_count = var.environment == "prod" ? 2 : 1
  api_desired_count           = var.environment == "prod" ? 2 : 1

  app_env           = var.environment
  s3_bucket         = module.storage.bucket
  s3_region         = var.region
  kinora_live_video = false # deliberate opt-in only
  cors_origins      = var.cors_origins

  secret_arns             = module.secrets.secret_arns
  task_execution_role_arn = module.secrets.execution_role_arn
  task_role_arn           = module.secrets.task_role_arn
}
