# ---------------------------------------------------------------------------- #
# storage module — OSS bucket for clips, keyframes, audio, locked refs, and the
# canon vault. Private + versioned + SSE; lifecycle tiers cold objects and
# expires aborted multipart uploads so the bucket doesn't accrete cost.
# The backend reaches it through the same S3-compatible boto3 ObjectStore used
# against MinIO locally.
# ---------------------------------------------------------------------------- #

variable "bucket_name" {
  description = "Globally-unique OSS bucket name."
  type        = string
}

variable "tags" {
  description = "Tags applied to the bucket."
  type        = map(string)
  default     = {}
}

variable "storage_class" {
  description = "Default OSS storage class."
  type        = string
  default     = "Standard"
}

variable "versioning" {
  description = "Enable object versioning (protects accepted clips / locked refs)."
  type        = bool
  default     = true
}

variable "ia_transition_days" {
  description = "Days after which noncurrent versions transition to Infrequent Access. 0 disables the lifecycle rule."
  type        = number
  default     = 30
}

variable "noncurrent_expiration_days" {
  description = "Days after which noncurrent versions are deleted. 0 disables expiry."
  type        = number
  default     = 90
}

resource "alicloud_oss_bucket" "this" {
  bucket        = var.bucket_name
  storage_class = var.storage_class
  tags          = var.tags

  # Lifecycle: tier then expire noncurrent versions, and clean aborted multipart
  # uploads so partial render uploads don't accrete storage cost. Inline because
  # this provider version models lifecycle as a block on the bucket (there is no
  # standalone alicloud_oss_bucket_lifecycle_rule resource).
  dynamic "lifecycle_rule" {
    for_each = var.ia_transition_days > 0 || var.noncurrent_expiration_days > 0 ? [1] : []
    content {
      id      = "kinora-lifecycle"
      prefix  = ""
      enabled = true

      abort_multipart_upload {
        days = 7
      }

      dynamic "noncurrent_version_transition" {
        for_each = var.versioning && var.ia_transition_days > 0 ? [1] : []
        content {
          days          = var.ia_transition_days
          storage_class = "IA"
        }
      }

      dynamic "noncurrent_version_expiration" {
        for_each = var.versioning && var.noncurrent_expiration_days > 0 ? [1] : []
        content {
          days = var.noncurrent_expiration_days
        }
      }
    }
  }
}

# Private bucket — the app serves objects via signed URLs.
resource "alicloud_oss_bucket_acl" "this" {
  bucket = alicloud_oss_bucket.this.bucket
  acl    = "private"
}

resource "alicloud_oss_bucket_versioning" "this" {
  count  = var.versioning ? 1 : 0
  bucket = alicloud_oss_bucket.this.bucket
  status = "Enabled"
}

# Server-side encryption at rest.
resource "alicloud_oss_bucket_server_side_encryption" "this" {
  bucket        = alicloud_oss_bucket.this.bucket
  sse_algorithm = "AES256"
}

output "bucket" {
  description = "OSS bucket name."
  value       = alicloud_oss_bucket.this.bucket
}

output "intranet_endpoint" {
  description = "OSS internal (intra-region) endpoint."
  value       = alicloud_oss_bucket.this.intranet_endpoint
}

output "extranet_endpoint" {
  description = "OSS public endpoint."
  value       = alicloud_oss_bucket.this.extranet_endpoint
}
