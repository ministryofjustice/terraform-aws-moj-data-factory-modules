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

  source_endpoint = {
    endpoint_id = var.source_endpoint_id
    engine_name = var.source_engine_name

    database_name = var.source_database_name

    secrets_manager_arn             = var.source_secrets_manager_arn
    secrets_manager_access_role_arn = var.source_secrets_manager_access_role_arn
    secrets_manager_kms_key_arn     = var.source_secrets_manager_kms_key_arn

    kms_key_arn     = var.source_endpoint_kms_key_arn
    certificate_arn = var.source_certificate_arn

    ssl_mode                    = var.source_ssl_mode
    extra_connection_attributes = var.source_extra_connection_attributes
  }

  s3_target_endpoint = {
    endpoint_id             = var.target_endpoint_id
    bucket_name             = var.target_bucket_name
    bucket_folder           = var.target_bucket_folder
    service_access_role_arn = var.target_service_access_role_arn

    encryption_mode                    = var.target_encryption_mode
    server_side_encryption_kms_key_arn = var.target_kms_key_arn
  }

  monitoring = {
    enabled = var.monitoring_enabled

    alarm_action_arns             = var.monitoring_alarm_action_arns
    ok_action_arns                = var.monitoring_ok_action_arns
    insufficient_data_action_arns = var.monitoring_insufficient_data_action_arns
  }

  tags = var.tags
}
