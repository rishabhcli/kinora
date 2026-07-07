# ECS app tier: api + ingest-worker + render-worker(s) + mcp. Each node runs the
# same backend container image with a different command (the real process model
# from docker-compose.yml), wired to RDS / Tair / OSS / DashScope via cloud-init.
#
# Alternative: run the stateless render-workers on Function Compute (event/queue
# triggered) instead of always-on ECS — see deploy/README.md. ECS is used here
# because it maps 1:1 to the local compose process model and is simplest to read.

locals {
  # Connection strings assembled from the managed-service outputs.
  database_url = format(
    "postgresql+asyncpg://%s:%s@%s:%s/%s",
    var.db_account_name,
    local.db_password,
    alicloud_db_instance.postgres.connection_string,
    alicloud_db_instance.postgres.port,
    var.db_name,
  )

  redis_url = format(
    "redis://:%s@%s:%s/0",
    local.redis_password,
    alicloud_kvstore_instance.redis.connection_domain,
    alicloud_kvstore_instance.redis.port,
  )

  # OSS exposes an S3-compatible endpoint; the app's boto3 ObjectStore targets it.
  oss_s3_endpoint = "https://oss-${var.region}.aliyuncs.com"

  # Common runtime env injected into every node's container. JWT_SECRET and
  # MCP_AUTH_TOKEN are shared across roles (the api verifies JWTs and calls MCP;
  # the mcp node requires the bearer; workers may call MCP too) — every node runs
  # the same image, matching docker-compose's shared x-backend env. CORS only
  # affects the api but is harmless elsewhere; render it as JSON because Pydantic
  # parses list env vars as JSON, not comma-separated strings.
  cloud_init_common = {
    image              = var.container_image
    build_from_source  = var.build_images_on_instance ? "true" : "false"
    source_repo_url    = var.source_repo_url
    source_ref         = var.source_ref
    app_env            = var.environment
    database_url       = local.database_url
    redis_url          = local.redis_url
    s3_endpoint_url    = local.oss_s3_endpoint
    s3_region          = var.region
    s3_bucket          = var.oss_bucket_name
    s3_access_key      = var.alicloud_access_key
    s3_secret_key      = var.alicloud_secret_key
    dashscope_api_key  = var.dashscope_api_key
    dashscope_base_url = var.dashscope_base_url
    kinora_live_video  = var.kinora_live_video ? "true" : "false"
    video_model        = var.video_model
    video_model_i2v    = var.video_model_i2v
    video_model_r2v    = var.video_model_r2v
    jwt_secret         = local.jwt_secret
    mcp_auth_token     = local.mcp_auth_token
    cors_origins       = jsonencode(var.cors_origins)
  }
}

resource "alicloud_instance" "api" {
  instance_name              = "${local.name}-api"
  host_name                  = "${local.name}-api"
  instance_type              = var.ecs_instance_type
  image_id                   = data.alicloud_images.app.images[0].id
  security_groups            = [alicloud_security_group.app.id]
  vswitch_id                 = alicloud_vswitch.this[0].id
  system_disk_category       = var.ecs_system_disk_category
  password                   = var.ecs_password
  internet_max_bandwidth_out = var.ecs_internet_bandwidth_out

  user_data = base64encode(templatefile("${path.module}/cloud-init.sh.tftpl", merge(local.cloud_init_common, {
    role    = "api"
    command = "uvicorn app.main:app --host 0.0.0.0 --port 8000"
    publish = "-p 8000:8000"
  })))

  tags = merge(local.common_tags, { Role = "api" })
}

resource "alicloud_instance" "frontend" {
  instance_name              = "${local.name}-frontend"
  host_name                  = "${local.name}-frontend"
  instance_type              = var.ecs_instance_type
  image_id                   = data.alicloud_images.app.images[0].id
  security_groups            = [alicloud_security_group.app.id]
  vswitch_id                 = alicloud_vswitch.this[0].id
  system_disk_category       = var.ecs_system_disk_category
  password                   = var.ecs_password
  internet_max_bandwidth_out = var.ecs_internet_bandwidth_out

  user_data = base64encode(templatefile("${path.module}/cloud-init-frontend.sh.tftpl", {
    image             = var.frontend_container_image
    build_from_source = var.build_images_on_instance ? "true" : "false"
    source_repo_url   = var.source_repo_url
    source_ref        = var.source_ref
    api_url           = "http://${alicloud_instance.api.private_ip}:8000"
  }))

  tags = merge(local.common_tags, { Role = "frontend" })
}

resource "alicloud_instance" "mcp" {
  instance_name              = "${local.name}-mcp"
  host_name                  = "${local.name}-mcp"
  instance_type              = var.ecs_instance_type
  image_id                   = data.alicloud_images.app.images[0].id
  security_groups            = [alicloud_security_group.app.id]
  vswitch_id                 = alicloud_vswitch.this[0].id
  system_disk_category       = var.ecs_system_disk_category
  password                   = var.ecs_password
  internet_max_bandwidth_out = var.ecs_internet_bandwidth_out

  user_data = base64encode(templatefile("${path.module}/cloud-init.sh.tftpl", merge(local.cloud_init_common, {
    role    = "mcp"
    command = "python -m app.mcp.run --http --host 0.0.0.0 --port 8765"
    publish = "-p 8765:8765"
  })))

  tags = merge(local.common_tags, { Role = "mcp" })
}

resource "alicloud_instance" "render_worker" {
  count                      = var.render_worker_count
  instance_name              = "${local.name}-render-worker-${count.index}"
  host_name                  = "${local.name}-render-worker-${count.index}"
  instance_type              = var.ecs_instance_type
  image_id                   = data.alicloud_images.app.images[0].id
  security_groups            = [alicloud_security_group.app.id]
  vswitch_id                 = alicloud_vswitch.this[count.index % length(alicloud_vswitch.this)].id
  system_disk_category       = var.ecs_system_disk_category
  password                   = var.ecs_password
  internet_max_bandwidth_out = var.ecs_internet_bandwidth_out

  user_data = base64encode(templatefile("${path.module}/cloud-init.sh.tftpl", merge(local.cloud_init_common, {
    role    = "render-worker"
    command = "python -m app.queue.worker"
    publish = ""
  })))

  tags = merge(local.common_tags, { Role = "render-worker" })
}

resource "alicloud_instance" "ingest_worker" {
  count                      = var.ingest_worker_count
  instance_name              = "${local.name}-ingest-worker-${count.index}"
  host_name                  = "${local.name}-ingest-worker-${count.index}"
  instance_type              = var.ecs_instance_type
  image_id                   = data.alicloud_images.app.images[0].id
  security_groups            = [alicloud_security_group.app.id]
  vswitch_id                 = alicloud_vswitch.this[count.index % length(alicloud_vswitch.this)].id
  system_disk_category       = var.ecs_system_disk_category
  password                   = var.ecs_password
  internet_max_bandwidth_out = var.ecs_internet_bandwidth_out

  user_data = base64encode(templatefile("${path.module}/cloud-init.sh.tftpl", merge(local.cloud_init_common, {
    role    = "ingest-worker"
    command = "python -m app.ingest.recovery"
    publish = ""
  })))

  tags = merge(local.common_tags, { Role = "ingest-worker" })
}
