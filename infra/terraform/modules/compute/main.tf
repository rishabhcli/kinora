locals {
  # Common runtime env injected into every node's container. Every node runs the
  # same image (matching docker-compose's shared x-backend env); JWT_SECRET +
  # MCP_AUTH_TOKEN are shared across roles. CORS only matters on api but is
  # harmless elsewhere; it's rendered as a comma list.
  cloud_init_common = {
    image              = var.container_image
    app_env            = var.app_env
    database_url       = var.database_url
    redis_url          = var.redis_url
    s3_endpoint_url    = var.s3_endpoint_url
    s3_region          = var.s3_region
    s3_bucket          = var.s3_bucket
    s3_access_key      = var.s3_access_key
    s3_secret_key      = var.s3_secret_key
    dashscope_api_key  = var.dashscope_api_key
    dashscope_base_url = var.dashscope_base_url
    kinora_live_video  = var.kinora_live_video ? "true" : "false"
    video_model        = var.video_model
    video_model_i2v    = var.video_model_i2v
    video_model_r2v    = var.video_model_r2v
    jwt_secret         = var.jwt_secret
    mcp_auth_token     = var.mcp_auth_token
    cors_origins       = join(",", var.cors_origins)
  }
}

# api — uvicorn (REST/SSE/WS; scheduler + ingest in-process).
resource "alicloud_instance" "api" {
  instance_name              = "${var.name}-api"
  host_name                  = "${var.name}-api"
  instance_type              = var.instance_type
  image_id                   = var.image_id
  security_groups            = [var.security_group_id]
  vswitch_id                 = var.vswitch_ids[0]
  system_disk_category       = var.system_disk_category
  password                   = var.ecs_password
  internet_max_bandwidth_out = var.internet_bandwidth_out

  user_data = base64encode(templatefile("${path.module}/templates/cloud-init.sh.tftpl", merge(local.cloud_init_common, {
    role    = "api"
    command = "uvicorn app.main:app --host 0.0.0.0 --port 8000"
    publish = "-p 8000:8000"
  })))

  tags = merge(var.tags, { Role = "api" })
}

# frontend — Nginx over the built Vite renderer.
resource "alicloud_instance" "frontend" {
  instance_name              = "${var.name}-frontend"
  host_name                  = "${var.name}-frontend"
  instance_type              = var.instance_type
  image_id                   = var.image_id
  security_groups            = [var.security_group_id]
  vswitch_id                 = var.vswitch_ids[0]
  system_disk_category       = var.system_disk_category
  password                   = var.ecs_password
  internet_max_bandwidth_out = var.internet_bandwidth_out

  user_data = base64encode(templatefile("${path.module}/templates/cloud-init-frontend.sh.tftpl", {
    image = var.frontend_container_image
  }))

  tags = merge(var.tags, { Role = "frontend" })
}

# mcp — the canon-memory MCP server (intra-VPC only by SG, bearer on top).
resource "alicloud_instance" "mcp" {
  instance_name              = "${var.name}-mcp"
  host_name                  = "${var.name}-mcp"
  instance_type              = var.instance_type
  image_id                   = var.image_id
  security_groups            = [var.security_group_id]
  vswitch_id                 = var.vswitch_ids[0]
  system_disk_category       = var.system_disk_category
  password                   = var.ecs_password
  internet_max_bandwidth_out = var.internet_bandwidth_out

  user_data = base64encode(templatefile("${path.module}/templates/cloud-init.sh.tftpl", merge(local.cloud_init_common, {
    role    = "mcp"
    command = "python -m app.mcp.run --http --host 0.0.0.0 --port 8765"
    publish = "-p 8765:8765"
  })))

  tags = merge(var.tags, { Role = "mcp" })
}

# render-worker(s) — drain the Redis priority queue; spread across vswitches.
resource "alicloud_instance" "render_worker" {
  count                      = var.render_worker_count
  instance_name              = "${var.name}-render-worker-${count.index}"
  host_name                  = "${var.name}-render-worker-${count.index}"
  instance_type              = var.instance_type
  image_id                   = var.image_id
  security_groups            = [var.security_group_id]
  vswitch_id                 = var.vswitch_ids[count.index % length(var.vswitch_ids)]
  system_disk_category       = var.system_disk_category
  password                   = var.ecs_password
  internet_max_bandwidth_out = var.internet_bandwidth_out

  user_data = base64encode(templatefile("${path.module}/templates/cloud-init.sh.tftpl", merge(local.cloud_init_common, {
    role    = "render-worker"
    command = "python -m app.queue.worker"
    publish = ""
  })))

  tags = merge(var.tags, { Role = "render-worker" })
}

# ingest-worker(s) — durable Phase-A recovery loop.
resource "alicloud_instance" "ingest_worker" {
  count                      = var.ingest_worker_count
  instance_name              = "${var.name}-ingest-worker-${count.index}"
  host_name                  = "${var.name}-ingest-worker-${count.index}"
  instance_type              = var.instance_type
  image_id                   = var.image_id
  security_groups            = [var.security_group_id]
  vswitch_id                 = var.vswitch_ids[count.index % length(var.vswitch_ids)]
  system_disk_category       = var.system_disk_category
  password                   = var.ecs_password
  internet_max_bandwidth_out = var.internet_bandwidth_out

  user_data = base64encode(templatefile("${path.module}/templates/cloud-init.sh.tftpl", merge(local.cloud_init_common, {
    role    = "ingest-worker"
    command = "python -m app.ingest.recovery"
    publish = ""
  })))

  tags = merge(var.tags, { Role = "ingest-worker" })
}
