terraform {
  required_version = ">= 1.3.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.42"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

module "dms_core" {
  source = "../../modules/dms-core"

  name   = var.name
  vpc_id = var.vpc_id

  replication_instance = {
    replication_instance_id    = var.replication_instance_id
    replication_instance_class = var.replication_instance_class
    allocated_storage          = var.allocated_storage

    engine_version = var.engine_version
    kms_key_arn    = var.kms_key_arn

    multi_az          = var.multi_az
    availability_zone = var.availability_zone

    subnet_ids = var.subnet_ids
  }

  tags = var.tags
}
