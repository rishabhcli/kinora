# Inputs the dev root needs to wire the stack. Sizing lives in main.tf; these are
# the credentials / network / secret knobs an operator supplies via tfvars or env.

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

variable "region" {
  type    = string
  default = "ap-southeast-1"
}

variable "zones" {
  type    = list(string)
  default = ["ap-southeast-1a"]
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "admin_cidr" {
  description = "CIDR allowed to reach API (8000) + frontend (80). Never 0.0.0.0/0."
  type        = string
}

variable "ssh_cidr" {
  description = "CIDR allowed to reach SSH (22). Never 0.0.0.0/0."
  type        = string
}

variable "oss_bucket_name" {
  type    = string
  default = "kinora-dev-assets"
}

variable "container_image" {
  type    = string
  default = "registry.ap-southeast-1.aliyuncs.com/kinora/backend:dev"
}

variable "frontend_container_image" {
  type    = string
  default = "registry.ap-southeast-1.aliyuncs.com/kinora/frontend:dev"
}

variable "dashscope_api_key" {
  type      = string
  default   = ""
  sensitive = true
}

variable "ecs_password" {
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
