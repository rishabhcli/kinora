variable "region" {
  type    = string
  default = "ap-southeast-1"
}

variable "environment" {
  description = "prod | staging | dev — drives HA/scale toggles."
  type        = string
  default     = "prod"
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "admin_cidr" {
  description = "CIDR allowed to reach the ALB (80/443). Never 0.0.0.0/0."
  type        = string
}

variable "s3_bucket_name" {
  type    = string
  default = "kinora-prod-assets"
}

variable "rds_instance_class" {
  type    = string
  default = "db.t3.medium"
}

variable "db_name" {
  type    = string
  default = "kinora"
}

variable "db_username" {
  type    = string
  default = "kinora"
}

variable "db_password" {
  type      = string
  sensitive = true
}

variable "redis_node_type" {
  type    = string
  default = "cache.t3.small"
}

variable "redis_auth_token" {
  description = "ElastiCache AUTH token (>= 16 chars)."
  type        = string
  sensitive   = true
}

variable "container_image" {
  type    = string
  default = "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/kinora/backend:latest"
}

variable "frontend_container_image" {
  type    = string
  default = "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/kinora/frontend:latest"
}

variable "dashscope_api_key" {
  type      = string
  default   = ""
  sensitive = true
}

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
  type = list(string)
}
