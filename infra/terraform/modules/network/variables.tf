# ---------------------------------------------------------------------------- #
# network module — VPC, per-zone vswitches, optional NAT, app/data security
# groups with the fail-closed ingress rules and the intra-VPC-only MCP rule.
# ---------------------------------------------------------------------------- #

variable "name" {
  description = "Resource name prefix (e.g. kinora-prod)."
  type        = string
}

variable "tags" {
  description = "Tags applied to taggable resources."
  type        = map(string)
  default     = {}
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.20.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid IPv4 CIDR."
  }
}

variable "zones" {
  description = "Availability zones for the vswitches."
  type        = list(string)

  validation {
    condition     = length(var.zones) > 0
    error_message = "Provide at least one availability zone."
  }
}

variable "vswitch_cidrs" {
  description = "CIDR blocks for the per-zone vswitches (must align 1:1 with zones)."
  type        = list(string)

  validation {
    condition     = length(var.vswitch_cidrs) == length(var.zones)
    error_message = "vswitch_cidrs must have exactly one entry per zone."
  }
}

variable "admin_cidr" {
  description = "CIDR allowed to reach the public API (8000) + frontend (80). Never 0.0.0.0/0."
  type        = string

  validation {
    condition     = var.admin_cidr != "0.0.0.0/0"
    error_message = "admin_cidr must not be 0.0.0.0/0 — lock it to your LB/office egress."
  }
}

variable "ssh_cidr" {
  description = "CIDR allowed to reach SSH (22). Ideally a bastion/VPN /32. Never 0.0.0.0/0."
  type        = string

  validation {
    condition     = var.ssh_cidr != "0.0.0.0/0"
    error_message = "ssh_cidr must not be 0.0.0.0/0 — use a bastion/VPN /32."
  }
}

variable "enable_nat" {
  description = "Provision a NAT gateway + EIP so private nodes (internet_max_bandwidth_out=0) can reach DashScope / pull images via the NAT instead of public IPs."
  type        = bool
  default     = false
}
