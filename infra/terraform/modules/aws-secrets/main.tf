# ---------------------------------------------------------------------------- #
# aws-secrets module — Secrets Manager entries for the runtime secrets + the ECS
# IAM roles. Secrets are auto-generated (random_password) when not supplied, then
# stored in Secrets Manager so the Fargate tasks pull them via `secrets` (never
# baked into the image or the task env in plaintext).
# ---------------------------------------------------------------------------- #

variable "name" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "database_url" {
  type      = string
  sensitive = true
}

variable "redis_url" {
  type      = string
  sensitive = true
}

variable "dashscope_api_key" {
  type      = string
  sensitive = true
}

variable "jwt_secret" {
  description = "Empty -> auto-generate."
  type        = string
  default     = ""
  sensitive   = true
}

variable "mcp_auth_token" {
  description = "Empty -> auto-generate."
  type        = string
  default     = ""
  sensitive   = true
}

variable "s3_bucket_arn" {
  description = "S3 bucket ARN the task role gets read/write on."
  type        = string
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
  jwt_secret     = var.jwt_secret != "" ? var.jwt_secret : random_password.jwt.result
  mcp_auth_token = var.mcp_auth_token != "" ? var.mcp_auth_token : random_password.mcp.result

  secret_values = {
    DATABASE_URL      = var.database_url
    REDIS_URL         = var.redis_url
    DASHSCOPE_API_KEY = var.dashscope_api_key
    JWT_SECRET        = local.jwt_secret
    MCP_AUTH_TOKEN    = local.mcp_auth_token
  }
}

resource "aws_secretsmanager_secret" "this" {
  for_each = local.secret_values
  name     = "${var.name}/${each.key}"
  tags     = var.tags
}

resource "aws_secretsmanager_secret_version" "this" {
  for_each      = local.secret_values
  secret_id     = aws_secretsmanager_secret.this[each.key].id
  secret_string = each.value
}

# -- ECS execution role: pull images, write logs, read the secrets ------------- #
data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.name}-ecs-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [for s in aws_secretsmanager_secret.this : s.arn]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "${var.name}-read-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

# -- ECS task role: S3 (OSS analogue) read/write on the assets bucket ---------- #
resource "aws_iam_role" "task" {
  name               = "${var.name}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "task_s3" {
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
    resources = [var.s3_bucket_arn, "${var.s3_bucket_arn}/*"]
  }
}

resource "aws_iam_role_policy" "task_s3" {
  name   = "${var.name}-task-s3"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_s3.json
}

output "secret_arns" {
  description = "Map of env var name -> Secrets Manager secret ARN."
  value       = { for k, s in aws_secretsmanager_secret.this : k => s.arn }
}

output "execution_role_arn" {
  value = aws_iam_role.execution.arn
}

output "task_role_arn" {
  value = aws_iam_role.task.arn
}

output "jwt_secret" {
  value     = local.jwt_secret
  sensitive = true
}

output "mcp_auth_token" {
  value     = local.mcp_auth_token
  sensitive = true
}
