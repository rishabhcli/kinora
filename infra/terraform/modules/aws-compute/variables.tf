# ---------------------------------------------------------------------------- #
# aws-compute module — ECS-on-Fargate. One task definition + service per role,
# all from the SAME backend image with a different command (the §process model):
#   api · ingest-worker · render-worker · mcp · frontend
# api + frontend sit behind an ALB (host/path routed); mcp is internal-only
# (registered in Cloud Map for service discovery, never on the ALB).
# ---------------------------------------------------------------------------- #

variable "name" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "region" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "alb_sg_id" {
  type = string
}

variable "app_sg_id" {
  type = string
}

variable "container_image" {
  type = string
}

variable "frontend_container_image" {
  type = string
}

# Fargate sizing (CPU units / MiB) per role.
variable "api_cpu" {
  type    = number
  default = 512
}

variable "api_memory" {
  type    = number
  default = 1024
}

variable "worker_cpu" {
  type    = number
  default = 1024
}

variable "worker_memory" {
  type    = number
  default = 2048
}

variable "render_worker_desired_count" {
  type    = number
  default = 1
}

variable "ingest_worker_desired_count" {
  type    = number
  default = 1
}

variable "api_desired_count" {
  type    = number
  default = 1
}

variable "log_retention_days" {
  type    = number
  default = 30
}

# Non-secret runtime env (ConfigMap analogue).
variable "app_env" {
  type = string
}

variable "s3_bucket" {
  type = string
}

variable "s3_region" {
  type = string
}

variable "dashscope_base_url" {
  type    = string
  default = "https://dashscope-intl.aliyuncs.com"
}

variable "kinora_live_video" {
  type    = bool
  default = false
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

variable "cors_origins" {
  type = list(string)

  validation {
    condition     = length(var.cors_origins) > 0 && !contains(var.cors_origins, "*")
    error_message = "cors_origins must be a non-empty list of explicit origins; no credentialed wildcard."
  }
}

# ARNs of Secrets Manager secrets injected into the task as `secrets`.
variable "secret_arns" {
  description = "Map of env var name -> Secrets Manager secret ARN (DATABASE_URL, REDIS_URL, DASHSCOPE_API_KEY, JWT_SECRET, MCP_AUTH_TOKEN, ...)."
  type        = map(string)
}

variable "task_execution_role_arn" {
  type = string
}

variable "task_role_arn" {
  type = string
}
