# Alibaba Cloud provider. Credentials and region come from variables so nothing
# secret is committed; pass them via terraform.tfvars (gitignored) or the
# ALICLOUD_ACCESS_KEY / ALICLOUD_SECRET_KEY / ALICLOUD_REGION environment vars.

provider "alicloud" {
  access_key = var.alicloud_access_key != "" ? var.alicloud_access_key : null
  secret_key = var.alicloud_secret_key != "" ? var.alicloud_secret_key : null
  region     = var.region
}
