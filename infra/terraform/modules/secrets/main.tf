# ---------------------------------------------------------------------------- #
# secrets module — resolve the four runtime secrets (db / redis / jwt / mcp).
# Each is the operator-provided value when non-empty, otherwise a strong
# auto-generated URL-safe password (so DSNs + HTTP bearers need no escaping).
# Centralising this keeps the resolution logic identical across every env.
# ---------------------------------------------------------------------------- #

variable "db_password" {
  description = "DB account password. Empty -> auto-generate."
  type        = string
  default     = ""
  sensitive   = true
}

variable "redis_password" {
  description = "Redis AUTH password. Empty -> auto-generate."
  type        = string
  default     = ""
  sensitive   = true
}

variable "jwt_secret" {
  description = "JWT signing secret. Empty -> auto-generate."
  type        = string
  default     = ""
  sensitive   = true
}

variable "mcp_auth_token" {
  description = "MCP HTTP bearer token. Empty -> auto-generate."
  type        = string
  default     = ""
  sensitive   = true
}

# URL-safe charset (letters/digits + - _) so the generated secrets drop cleanly
# into DATABASE_URL / REDIS_URL DSNs and HTTP bearer headers without escaping.
resource "random_password" "db" {
  length           = 24
  special          = true
  override_special = "-_"
}

resource "random_password" "redis" {
  length           = 24
  special          = true
  override_special = "-_"
}

resource "random_password" "jwt" {
  length           = 48
  special          = true
  override_special = "-_"
}

resource "random_password" "mcp" {
  length           = 48
  special          = true
  override_special = "-_"
}

locals {
  db_password    = var.db_password != "" ? var.db_password : random_password.db.result
  redis_password = var.redis_password != "" ? var.redis_password : random_password.redis.result
  jwt_secret     = var.jwt_secret != "" ? var.jwt_secret : random_password.jwt.result
  mcp_auth_token = var.mcp_auth_token != "" ? var.mcp_auth_token : random_password.mcp.result
}

output "db_password" {
  value     = local.db_password
  sensitive = true
}

output "redis_password" {
  value     = local.redis_password
  sensitive = true
}

output "jwt_secret" {
  value     = local.jwt_secret
  sensitive = true
}

output "mcp_auth_token" {
  value     = local.mcp_auth_token
  sensitive = true
}
