# ---------------------------------------------------------------------------- #
# compute module — the per-role ECS fleet. Every node runs the SAME backend image
# with a different command (the real process model from docker-compose.yml):
#   api · ingest-worker · render-worker(s) · mcp   (+ a separate frontend node)
# Wired to RDS / Tair / OSS / DashScope via cloud-init.
# ---------------------------------------------------------------------------- #

variable "name" {
  description = "Resource name prefix."
  type        = string
}

variable "tags" {
  description = "Base tags (each node also gets a Role tag)."
  type        = map(string)
  default     = {}
}

variable "image_id" {
  description = "ECS system image id (resolved by the caller)."
  type        = string
}

variable "instance_type" {
  description = "ECS instance type for the app nodes (region-specific)."
  type        = string
}

variable "security_group_id" {
  description = "App-tier security group id."
  type        = string
}

variable "vswitch_ids" {
  description = "vswitch ids to spread nodes across (round-robin)."
  type        = list(string)
}

variable "system_disk_category" {
  description = "ECS system disk category."
  type        = string
  default     = "cloud_essd"
}

variable "ecs_password" {
  description = "Root/login password for the ECS instances."
  type        = string
  sensitive   = true
}

variable "internet_bandwidth_out" {
  description = "Public egress bandwidth (Mbps). 0 = private (then use the NAT gateway)."
  type        = number
  default     = 10
}

variable "render_worker_count" {
  description = "Number of render-worker nodes."
  type        = number
  default     = 1
}

variable "ingest_worker_count" {
  description = "Number of ingest recovery-worker nodes."
  type        = number
  default     = 1
}

variable "container_image" {
  description = "Fully-qualified Kinora backend image."
  type        = string
}

variable "frontend_container_image" {
  description = "Fully-qualified Kinora web renderer image."
  type        = string
}

# -- App runtime env (rendered into each node's /etc/kinora/kinora.env) --------- #

variable "app_env" {
  description = "APP_ENV label (prod | staging | dev)."
  type        = string
}

variable "database_url" {
  description = "Assembled async SQLAlchemy DSN."
  type        = string
  sensitive   = true
}

variable "redis_url" {
  description = "Assembled Redis URL."
  type        = string
  sensitive   = true
}

variable "s3_endpoint_url" {
  description = "S3-compatible OSS endpoint."
  type        = string
}

variable "s3_region" {
  description = "OSS region."
  type        = string
}

variable "s3_bucket" {
  description = "OSS bucket name."
  type        = string
}

variable "s3_access_key" {
  description = "OSS access key (Alibaba AK)."
  type        = string
  sensitive   = true
}

variable "s3_secret_key" {
  description = "OSS secret key (Alibaba secret)."
  type        = string
  sensitive   = true
}

variable "dashscope_api_key" {
  description = "DashScope (Model Studio) intl API key."
  type        = string
  sensitive   = true
}

variable "dashscope_base_url" {
  description = "DashScope base URL (intl endpoint)."
  type        = string
  default     = "https://dashscope-intl.aliyuncs.com"
}

variable "kinora_live_video" {
  description = "Go-live gate for real Wan spend. Keep false unless intentional."
  type        = bool
  default     = false
}

variable "video_model" {
  type    = string
  default = "wan2.7-t2v"
}

variable "video_model_i2v" {
  type    = string
  default = "wan2.7-i2v"
}

variable "video_model_r2v" {
  type    = string
  default = "wan2.7-i2v"
}

variable "jwt_secret" {
  description = "Resolved JWT signing secret."
  type        = string
  sensitive   = true
}

variable "mcp_auth_token" {
  description = "Resolved MCP bearer token."
  type        = string
  sensitive   = true
}

variable "cors_origins" {
  description = "Browser origins allowed under credentialed CORS (no wildcard)."
  type        = list(string)
}
