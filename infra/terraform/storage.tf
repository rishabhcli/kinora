# OSS bucket for clips, keyframes, audio, locked references, and the canon vault.
# The backend reaches it through the same S3-compatible boto3 ObjectStore used
# everywhere (MinIO locally / OSS in prod), via the region S3 endpoint.

resource "alicloud_oss_bucket" "assets" {
  bucket        = var.oss_bucket_name
  storage_class = "Standard"
  tags          = local.common_tags
}

# Keep the bucket private; the app serves objects via signed URLs.
resource "alicloud_oss_bucket_acl" "assets" {
  bucket = alicloud_oss_bucket.assets.bucket
  acl    = "private"
}

# Versioning protects accepted clips / locked references from accidental overwrite.
resource "alicloud_oss_bucket_versioning" "assets" {
  bucket = alicloud_oss_bucket.assets.bucket
  status = "Enabled"
}

# Server-side encryption at rest.
resource "alicloud_oss_bucket_server_side_encryption" "assets" {
  bucket        = alicloud_oss_bucket.assets.bucket
  sse_algorithm = "AES256"
}
