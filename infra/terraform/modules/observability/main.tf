# ---------------------------------------------------------------------------- #
# observability module — CloudMonitor alarm group + an SLS (Log Service) project
# and logstore for centralised app/container logs. Optional (toggle per env) so
# dev can stay lean. The app's own Prometheus metrics (§12.5) are scraped by the
# in-cluster / compose Prometheus; this covers host-level + log aggregation that
# the cloud provides natively.
# ---------------------------------------------------------------------------- #

variable "name" {
  description = "Resource name prefix."
  type        = string
}

variable "tags" {
  description = "Tags applied to taggable resources."
  type        = map(string)
  default     = {}
}

variable "enabled" {
  description = "Provision the observability resources (alarm group + SLS)."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "SLS logstore retention in days."
  type        = number
  default     = 30
}

variable "alarm_contact_groups" {
  description = "CloudMonitor contact groups that receive alarm notifications."
  type        = list(string)
  default     = []
}

# A monitor group to attach ECS host metrics + alarm rules to.
resource "alicloud_cms_monitor_group" "this" {
  count              = var.enabled ? 1 : 0
  monitor_group_name = "${var.name}-monitor"
}

# Centralised log project + a single logstore for container stdout/stderr shipped
# via the Logtail agent (configured out of band or by a future cloud-init step).
resource "alicloud_log_project" "this" {
  count        = var.enabled ? 1 : 0
  project_name = "${var.name}-logs"
  description  = "Kinora centralised logs (${var.name})"
  tags         = var.tags
}

resource "alicloud_log_store" "app" {
  count                 = var.enabled ? 1 : 0
  project_name          = alicloud_log_project.this[0].project_name
  logstore_name         = "app"
  shard_count           = 2
  retention_period      = var.log_retention_days
  auto_split            = true
  max_split_shard_count = 8
}

output "monitor_group_id" {
  value = var.enabled ? alicloud_cms_monitor_group.this[0].id : ""
}

output "log_project" {
  value = var.enabled ? alicloud_log_project.this[0].project_name : ""
}

output "log_store" {
  value = var.enabled ? alicloud_log_store.app[0].logstore_name : ""
}
