terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5.0"
    }
  }

  # Remote state for teams: an S3 backend + DynamoDB lock. Left commented so
  # `terraform init -backend=false` validates with no remote dependency.
  #
  # backend "s3" {
  #   bucket         = "kinora-tfstate"
  #   key            = "aws-prod/terraform.tfstate"
  #   region         = "ap-southeast-1"
  #   dynamodb_table = "kinora-tflock"
  #   encrypt        = true
  # }
}
