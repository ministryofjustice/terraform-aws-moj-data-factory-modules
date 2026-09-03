variable "aws_region" {
  description = "AWS region used by the example."
  type        = string
  default     = "eu-west-2"
}

variable "name" {
  description = "Stable name used for the example DMS infrastructure."
  type        = string
  default     = "example-dms-core"
}

variable "vpc_id" {
  description = "Existing VPC ID in which the DMS replication infrastructure will be created."
  type        = string
}

variable "subnet_ids" {
  description = "Existing subnet IDs used to create the DMS replication subnet group."
  type        = list(string)
}

variable "replication_instance_id" {
  description = "Identifier for the example DMS replication instance."
  type        = string
  default     = "example-dms-core"
}

variable "replication_instance_class" {
  description = "AWS DMS replication instance class."
  type        = string
  default     = "dms.t3.medium"
}

variable "allocated_storage" {
  description = "Storage allocated to the DMS replication instance in GiB."
  type        = number
  default     = 50
}

variable "engine_version" {
  description = "Optional AWS DMS engine version."
  type        = string
  default     = null
}

variable "kms_key_arn" {
  description = "Optional KMS key ARN used to encrypt the DMS replication instance."
  type        = string
  default     = null
}

variable "multi_az" {
  description = "Whether the DMS replication instance should use Multi-AZ."
  type        = bool
  default     = false
}

variable "availability_zone" {
  description = "Optional Availability Zone for a single-AZ DMS replication instance."
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags applied to resources created by the example."
  type        = map(string)

  default = {
    application = "dms-core-example"
  }
}

variable "source_endpoint_id" {
  description = "Identifier for the example DMS source endpoint."
  type        = string
  default     = "example-dms-core-source"
}

variable "source_engine_name" {
  description = "Source database engine used by the example."
  type        = string
  default     = "postgres"
}

variable "source_database_name" {
  description = "Database name configured on the DMS source endpoint."
  type        = string
}

variable "source_secrets_manager_arn" {
  description = "ARN of the Secrets Manager secret used by DMS for source connectivity."
  type        = string
}

variable "source_secrets_manager_access_role_arn" {
  description = "ARN of the IAM role that allows DMS to access the source secret."
  type        = string
}

variable "source_endpoint_kms_key_arn" {
  description = "Optional KMS key ARN used by DMS for source endpoint connection parameters."
  type        = string
  default     = null
}

variable "source_certificate_arn" {
  description = "Optional DMS certificate ARN used when the configured SSL mode requires certificate verification."
  type        = string
  default     = null
}

variable "source_ssl_mode" {
  description = "SSL mode used by the DMS source endpoint."
  type        = string
  default     = "none"
}

variable "source_extra_connection_attributes" {
  description = "Optional engine-specific DMS source endpoint connection attributes."
  type        = string
  default     = null
}

variable "target_endpoint_id" {
  description = "Identifier for the example DMS S3 target endpoint."
  type        = string
  default     = "example-dms-core-target"
}

variable "target_bucket_name" {
  description = "Existing S3 bucket used as the DMS target."
  type        = string
}

variable "target_bucket_folder" {
  description = "Optional folder/prefix within the target S3 bucket."
  type        = string
  default     = null
}

variable "target_service_access_role_arn" {
  description = "ARN of the IAM role used by DMS to access the target S3 bucket."
  type        = string
}

variable "target_encryption_mode" {
  description = "Server-side encryption mode used by the DMS S3 target."
  type        = string
  default     = "SSE_S3"
}

variable "target_kms_key_id" {
  description = "KMS key identifier used by the S3 target when target_encryption_mode is SSE_KMS."
  type        = string
  default     = null
}
