output "vpc_id" {
  description = "VPC id."
  value       = alicloud_vpc.this.id
}

output "vpc_cidr" {
  description = "VPC CIDR block."
  value       = alicloud_vpc.this.cidr_block
}

output "vswitch_ids" {
  description = "Per-zone vswitch ids (ordered to match var.zones)."
  value       = alicloud_vswitch.this[*].id
}

output "app_security_group_id" {
  description = "App-tier security group id."
  value       = alicloud_security_group.app.id
}

output "data_security_group_id" {
  description = "Data-tier security group id (intra-VPC only)."
  value       = alicloud_security_group.data.id
}

output "nat_eip" {
  description = "NAT gateway EIP (empty when enable_nat = false)."
  value       = var.enable_nat ? alicloud_eip_address.nat[0].ip_address : ""
}
