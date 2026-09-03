variable "name" {
  description = "Stable name used to identify the DMS ingestion infrastructure."
  type        = string

  validation {
    condition     = length(trimspace(var.name)) > 0
    error_message = "name must not be empty."
  }
}

variable "vpc_id" {
  description = "VPC in which the DMS replication infrastructure is deployed."
  type        = string

  validation {
    condition     = length(trimspace(var.vpc_id)) > 0
    error_message = "vpc_id must not be empty."
  }
}

variable "replication_instance" {
  description = <<-EOT
    Configuration for the AWS DMS replication instance.

    For replication subnet configuration exactly one of the following approaches
    must be used:

      - provide existing_replication_subnet_group_id to use an existing DMS
        replication subnet group

        or

      - provide at least two subnet_ids and allow this module to create the DMS
        replication subnet group

    replication_subnet_group_name is used only when this module creates the subnet
    group. If omitted name is used.

    engine_version is intentionally not restricted to a hard-coded allow list.
    AWS DMS and the AWS provider remain authoritative for supported engine versions.
  EOT

  type = object({
    replication_instance_id    = string
    replication_instance_class = string
    allocated_storage          = number

    engine_version = optional(string)
    kms_key_arn    = optional(string)

    multi_az          = optional(bool, false)
    availability_zone = optional(string)

    apply_immediately            = optional(bool, false)
    auto_minor_version_upgrade   = optional(bool, true)
    preferred_maintenance_window = optional(string, "sun:10:30-sun:14:30")

    existing_replication_subnet_group_id = optional(string)
    replication_subnet_group_name        = optional(string)
    subnet_ids                           = optional(list(string))
  })

  validation {
    condition     = length(trimspace(var.replication_instance.replication_instance_id)) > 0
    error_message = "replication_instance.replication_instance_id must not be empty."
  }

  validation {
    condition     = length(trimspace(var.replication_instance.replication_instance_class)) > 0
    error_message = "replication_instance.replication_instance_class must not be empty."
  }

  validation {
    condition = (
      var.replication_instance.existing_replication_subnet_group_id != null
      ||
      try(length(var.replication_instance.subnet_ids), 0) >= 2
    )

    error_message = "Provide either existing_replication_subnet_group_id or at least two subnet_ids."
  }

  validation {
    condition = !(
      var.replication_instance.existing_replication_subnet_group_id != null
      &&
      try(length(var.replication_instance.subnet_ids), 0) > 0
    )

    error_message = "Provide either existing_replication_subnet_group_id or subnet_ids not both."
  }

  validation {
    condition = !(
      var.replication_instance.multi_az
      &&
      var.replication_instance.availability_zone != null
    )

    error_message = "replication_instance.availability_zone must not be supplied when replication_instance.multi_az is true."
  }

  validation {
    condition     = var.replication_instance.allocated_storage > 0
    error_message = "replication_instance.allocated_storage must be greater than zero."
  }

  validation {
    condition = (
      var.replication_instance.replication_subnet_group_name == null
      ||
      var.replication_instance.existing_replication_subnet_group_id == null
    )

    error_message = "replication_instance.replication_subnet_group_name must not be supplied when using existing_replication_subnet_group_id."
  }
}

variable "security_group" {
  description = <<-EOT
    Security-group configuration for the DMS replication instance.

    This module always creates a dedicated security group for the replication instance.

    allow_all_egress defaults to true to preserve the connectivity behaviour
    of the existing DE DMS implementation.

    Consumers with a stricter network model can disable that rule and attach
    additional externally managed VPC security groups using
    additional_vpc_security_group_ids.

    Source-specific ingress/egress policy is intentionally not modelled by this
    module. Consumers remain responsible for providing the network connectivity
    required between the DMS replication instance and the configured source.
  EOT

  type = object({
    allow_all_egress                  = optional(bool, true)
    additional_vpc_security_group_ids = optional(list(string), [])
  })

  default = {}
}

variable "tags" {
  description = "Tags applied to resources created by this module."
  type        = map(string)
  default     = {}
}

variable "source_endpoint" {
  description = <<-EOT
    Configuration for the AWS DMS source endpoint.

    The source endpoint supports PostgreSQL and Oracle.

    Authentication is provided through AWS Secrets Manager. The caller supplies
    the secret ARN and the IAM role ARN that AWS DMS uses to access the secret.

    The module does not read or decode the secret contents itself.

    database_name remains explicit because it is part of the DMS endpoint
    configuration rather than a credential.

    Engine-specific DMS behaviour can be supplied through
    extra_connection_attributes where required without embedding Data Hub-specific
    assumptions into this module.
  EOT

  type = object({
    endpoint_id = string
    engine_name = string

    database_name = string

    secrets_manager_arn             = string
    secrets_manager_access_role_arn = string

    kms_key_arn     = optional(string)
    certificate_arn = optional(string)

    ssl_mode                    = optional(string, "none")
    extra_connection_attributes = optional(string)
  })

  validation {
    condition     = contains(["oracle", "postgres"], var.source_endpoint.engine_name)
    error_message = "source_endpoint.engine_name must be either 'oracle' or 'postgres'."
  }

  validation {
    condition     = length(trimspace(var.source_endpoint.endpoint_id)) > 0
    error_message = "source_endpoint.endpoint_id must not be empty."
  }

  validation {
    condition     = length(trimspace(var.source_endpoint.database_name)) > 0
    error_message = "source_endpoint.database_name must not be empty."
  }

  validation {
    condition     = length(trimspace(var.source_endpoint.secrets_manager_arn)) > 0
    error_message = "source_endpoint.secrets_manager_arn must not be empty."
  }

  validation {
    condition     = length(trimspace(var.source_endpoint.secrets_manager_access_role_arn)) > 0
    error_message = "source_endpoint.secrets_manager_access_role_arn must not be empty."
  }

  validation {
    condition = (
      !contains(["verify-ca", "verify-full"], var.source_endpoint.ssl_mode)
      ||
      var.source_endpoint.certificate_arn != null
    )

    error_message = "source_endpoint.certificate_arn must be supplied when ssl_mode is 'verify-ca' or 'verify-full'."
  }
}

variable "s3_target_endpoint" {
  description = <<-EOT
    Configuration for the AWS DMS S3 target endpoint.

    The target bucket and service-access role are supplied by the caller.

    This module configures DMS to write to the supplied S3 landing location but
    does not create or manage the wider Raw/Raw History storage lifecycle.

    S3 target settings are configurable so consumers can override DMS defaults
    without introducing Data Hub-specific assumptions into the reusable module.
  EOT

  type = object({
    endpoint_id             = string
    bucket_name             = string
    service_access_role_arn = string

    bucket_folder = optional(string)

    add_column_name        = optional(bool, true)
    cdc_max_batch_interval = optional(number, 3600)
    cdc_min_file_size      = optional(number, 32000)

    compression_type = optional(string, "GZIP")
    data_format      = optional(string, "parquet")
    encoding_type    = optional(string, "rle_dictionary")

    encryption_mode                   = optional(string, "SSE_S3")
    server_side_encryption_kms_key_id = optional(string)

    include_op_for_full_load         = optional(bool, true)
    parquet_timestamp_in_millisecond = optional(bool, true)
    parquet_version                  = optional(string, "parquet-2-0")
    timestamp_column_name            = optional(string, "EXTRACTION_TIMESTAMP")
  })

  validation {
    condition     = length(trimspace(var.s3_target_endpoint.endpoint_id)) > 0
    error_message = "s3_target_endpoint.endpoint_id must not be empty."
  }

  validation {
    condition     = length(trimspace(var.s3_target_endpoint.bucket_name)) > 0
    error_message = "s3_target_endpoint.bucket_name must not be empty."
  }

  validation {
    condition     = length(trimspace(var.s3_target_endpoint.service_access_role_arn)) > 0
    error_message = "s3_target_endpoint.service_access_role_arn must not be empty."
  }

  validation {
    condition = (
      var.s3_target_endpoint.encryption_mode != "SSE_KMS"
      ||
      try(
        length(trimspace(var.s3_target_endpoint.server_side_encryption_kms_key_id)) > 0,
        false
      )
    )

    error_message = "s3_target_endpoint.server_side_encryption_kms_key_id must be supplied when encryption_mode is 'SSE_KMS'."
  }

  validation {
    condition     = var.s3_target_endpoint.cdc_max_batch_interval > 0
    error_message = "s3_target_endpoint.cdc_max_batch_interval must be greater than zero."
  }

  validation {
    condition     = var.s3_target_endpoint.cdc_min_file_size > 0
    error_message = "s3_target_endpoint.cdc_min_file_size must be greater than zero."
  }

  validation {
    condition = (
      var.s3_target_endpoint.encryption_mode == "SSE_KMS"
      ||
      var.s3_target_endpoint.server_side_encryption_kms_key_id == null
    )

    error_message = "s3_target_endpoint.server_side_encryption_kms_key_id must only be supplied when encryption_mode is 'SSE_KMS'."
  }
}
