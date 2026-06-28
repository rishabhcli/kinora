terraform {
  required_version = ">= 1.5.0"

  required_providers {
    alicloud = {
      source  = "aliyun/alicloud"
      version = ">= 1.230.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5.0"
    }
  }

  # Remote state for teams: an OSS backend keyed per environment. Left commented
  # so `terraform init -backend=false` validates with no remote dependency.
  #
  # backend "oss" {
  #   bucket = "kinora-tfstate"
  #   prefix = "staging/terraform.tfstate"
  #   region = "ap-southeast-1"
  #   # tablestore_endpoint / tablestore_table for state locking.
  # }
}
