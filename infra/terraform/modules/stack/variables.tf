# ---------------------------------------------------------------------------- #
# stack module — the full Kinora Alibaba footprint composed from the building-
# block modules (network, secrets, storage, database, redis, compute,
# observability). Each environment (dev/staging/prod) is a thin root that calls
# this module with environment-specific sizing via tfvars, so the topology is
# defined ONCE and the only drift between envs is intentional sizing.
# ---------------------------------------------------------------------------- #

# -- Identity / tagging -------------------------------------------------------- #
variable "project" {
  type    = string
  default = "kinora"
}

variable "environment" {
  description = "Deployment environment label (prod | staging | dev)."
  type        = string
}

variable "region" {
  type    = string
  default = "ap-southeast-1"
}

variable "tags" {
  type    = map(string)
  default = {}
}

# -- Network ------------------------------------------------------------------- #
variable "zones" {
  type = list(string)
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "vswitch_cidrs" {
  type = list(string)
}

variable "admin_cidr" {
  description = "CIDR allowed to reach API (8000) + frontend (80). Never 0.0.0.0/0."
  type        = string
}

variable "ssh_cidr" {
  description = "CIDR allowed to reach SSH (22). Never 0.0.0.0/0."
  type        = string
}

variable "enable_nat" {
  type    = bool
  default = false
}

# -- Object storage ------------------------------------------------------------ #
variable "oss_bucket_name" {
  type = string
}

variable "oss_ia_transition_days" {
  type    = number
  default = 30
}

variable "oss_noncurrent_expiration_days" {
  type    = number
  default = 90
}

# -- Database ------------------------------------------------------------------ #
variable "rds_engine_version" {
  type    = string
  default = "16.0"
}

variable "rds_instance_type" {
  type = string
}

variable "rds_instance_storage" {
  type    = number
  default = 50
}

variable "rds_storage_type" {
  type    = string
  default = "cloud_essd"
}

variable "rds_backup_retention_days" {
  type    = number
  default = 7
}

variable "rds_high_availability" {
  type    = bool
  default = false
}

variable "db_name" {
  type    = string
  default = "kinora"
}

variable "db_account_name" {
  type    = string
  default = "kinora"
}

variable "db_account_password" {
  type      = string
  default   = ""
  sensitive = true
}

# -- Redis --------------------------------------------------------------------- #
variable "redis_instance_class" {
  type = string
}

variable "redis_engine_version" {
  type    = string
  default = "7.0"
}

variable "redis_password" {
  type      = string
  default   = ""
  sensitive = true
}

# -- Compute ------------------------------------------------------------------- #
variable "ecs_instance_type" {
  type = string
}

variable "ecs_password" {
  type      = string
  default   = ""
  sensitive = true
}

variable "ecs_system_disk_category" {
  type    = string
  default = "cloud_essd"
}

variable "ecs_internet_bandwidth_out" {
  type    = number
  default = 10
}

variable "render_worker_count" {
  type    = number
  default = 1
}

variable "ingest_worker_count" {
  type    = number
  default = 1
}

# -- Application runtime ------------------------------------------------------- #
variable "container_image" {
  type = string
}

variable "frontend_container_image" {
  type = string
}

variable "dashscope_api_key" {
  type      = string
  default   = ""
  sensitive = true
}

variable "dashscope_base_url" {
  type    = string
  default = "https://dashscope-intl.aliyuncs.com"
}

variable "kinora_live_video" {
  description = "Go-live gate (kinora.md §11.1). Keep false unless intentional."
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

# Alibaba AK/secret are reused as the OSS S3 credentials inside the app.
variable "alicloud_access_key" {
  type      = string
  default   = ""
  sensitive = true
}

variable "alicloud_secret_key" {
  type      = string
  default   = ""
  sensitive = true
}

# -- Auth / CORS --------------------------------------------------------------- #
variable "jwt_secret" {
  type      = string
  default   = ""
  sensitive = true
}

variable "mcp_auth_token" {
  type      = string
  default   = ""
  sensitive = true
}

variable "cors_origins" {
  description = "Browser origin(s) allowed under credentialed CORS. Non-empty, no wildcard."
  type        = list(string)

  validation {
    condition     = length(var.cors_origins) > 0 && !contains(var.cors_origins, "*")
    error_message = "cors_origins must be a non-empty list of explicit origins; a credentialed wildcard '*' is not allowed."
  }
}

# -- Observability ------------------------------------------------------------- #
variable "observability_enabled" {
  type    = bool
  default = true
}

variable "log_retention_days" {
  type    = number
  default = 30
}
