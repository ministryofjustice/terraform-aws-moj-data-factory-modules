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

variable "monitoring" {
  description = <<-EOT
    CloudWatch monitoring configuration for the DMS replication instance.

    Monitoring is enabled by default. Alarm destinations are supplied by the
    caller so this module does not own SNS topics, Slack integrations or other
    notification infrastructure.

    Thresholds are configurable to avoid embedding environment-specific
    policy in the reusable module.
  EOT

  type = object({
    enabled = optional(bool, true)

    alarm_action_arns             = optional(list(string), [])
    ok_action_arns                = optional(list(string), [])
    insufficient_data_action_arns = optional(list(string), [])

    cpu_utilization_threshold          = optional(number, 80)
    free_storage_space_threshold_bytes = optional(number, 10737418240)
    freeable_memory_threshold_bytes    = optional(number, 1073741824)

    period_seconds     = optional(number, 300)
    evaluation_periods = optional(number, 3)
  })

  default = {}

  validation {
    condition = (
      var.monitoring.cpu_utilization_threshold > 0
      &&
      var.monitoring.cpu_utilization_threshold <= 100
    )

    error_message = "monitoring.cpu_utilization_threshold must be greater than zero and no greater than 100."
  }

  validation {
    condition     = var.monitoring.free_storage_space_threshold_bytes > 0
    error_message = "monitoring.free_storage_space_threshold_bytes must be greater than zero."
  }

  validation {
    condition     = var.monitoring.freeable_memory_threshold_bytes > 0
    error_message = "monitoring.freeable_memory_threshold_bytes must be greater than zero."
  }

  validation {
    condition     = var.monitoring.period_seconds > 0
    error_message = "monitoring.period_seconds must be greater than zero."
  }

  validation {
    condition     = var.monitoring.evaluation_periods > 0
    error_message = "monitoring.evaluation_periods must be greater than zero."
  }
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

    Authentication is provided through AWS Secrets Manager.
    The caller supplies the secret ARN and may provide an existing
    IAM role ARN for AWS DMS to use.

    If no access role is supplied the module creates a least-privilege role for
    AWS DMS to access the source secret.

    The module does not read or decode the secret contents itself.

    database_name remains explicit because it is part of the DMS endpoint
    configuration rather than a credential.

    Native PostgreSQL endpoint behaviour can be configured through the optional
    postgres_settings object. Other engine-specific DMS behaviour can be supplied
    through extra_connection_attributes where required without embedding
    Data Hub specific assumptions into this module.
  EOT

  type = object({
    endpoint_id = string
    engine_name = string

    database_name = string

    secrets_manager_arn             = string
    secrets_manager_access_role_arn = optional(string)
    secrets_manager_kms_key_arn     = optional(string)

    kms_key_arn     = optional(string)
    certificate_arn = optional(string)

    ssl_mode                    = optional(string, "none")
    extra_connection_attributes = optional(string)

    postgres_settings = optional(object({
      map_boolean_as_boolean       = optional(bool)
      fail_tasks_on_lob_truncation = optional(bool)
      heartbeat_enable             = optional(bool)
      heartbeat_frequency          = optional(number)
    }))

    oracle_settings = optional(object({
      secrets_manager_oracle_asm_secret_arn  = string
      secrets_manager_oracle_asm_kms_key_arn = optional(string)
    }))
  })

  validation {
    condition     = contains(["oracle", "postgres"], var.source_endpoint.engine_name)
    error_message = "source_endpoint.engine_name must be either 'oracle' or 'postgres'."
  }

  validation {
    condition = (
      var.source_endpoint.postgres_settings == null
      ||
      var.source_endpoint.engine_name == "postgres"
    )

    error_message = "source_endpoint.postgres_settings may only be supplied when source_endpoint.engine_name is 'postgres'."
  }

  validation {
    condition = (
      var.source_endpoint.postgres_settings == null
      ||
      var.source_endpoint.postgres_settings.heartbeat_frequency == null
      ||
      var.source_endpoint.postgres_settings.heartbeat_frequency > 0
    )

    error_message = "source_endpoint.postgres_settings.heartbeat_frequency must be greater than zero when supplied."
  }

  validation {
    condition = (
      var.source_endpoint.oracle_settings == null
      ||
      var.source_endpoint.engine_name == "oracle"
    )
    error_message = "source_endpoint.oracle_settings may only be supplied when source_endpoint.engine_name is 'oracle'."
  }

  validation {
    condition = (
      var.source_endpoint.oracle_settings == null
      ||
      length(trimspace(var.source_endpoint.oracle_settings.secrets_manager_oracle_asm_secret_arn)) > 0
    )

    error_message = "source_endpoint.oracle_settings.secrets_manager_oracle_asm_secret_arn must be non-empty when oracle_settings is supplied."
  }

  validation {
    condition = (
      var.source_endpoint.oracle_settings == null
      ||
      var.source_endpoint.oracle_settings.secrets_manager_oracle_asm_kms_key_arn == null
      ||
      length(trimspace(var.source_endpoint.oracle_settings.secrets_manager_oracle_asm_kms_key_arn)) > 0
    )

    error_message = "source_endpoint.oracle_settings.secrets_manager_oracle_asm_kms_key_arn must be null or a non-empty string."
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
    condition = (
      var.source_endpoint.secrets_manager_access_role_arn == null
      ||
      length(trimspace(var.source_endpoint.secrets_manager_access_role_arn)) > 0
    )

    error_message = "source_endpoint.secrets_manager_access_role_arn must be null or a non-empty string."
  }

  validation {
    condition = (
      var.source_endpoint.secrets_manager_kms_key_arn == null
      ||
      length(trimspace(var.source_endpoint.secrets_manager_kms_key_arn)) > 0
    )

    error_message = "source_endpoint.secrets_manager_kms_key_arn must be null or a non-empty string."
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

    The target bucket is supplied by the caller.
    The caller may provide an existing DMS service-access role. If no role is
    supplied the module creates a least-privilege role for AWS DMS to access
    the target bucket.

    This module configures DMS to write to the supplied S3 landing location but
    does not create or manage the wider Raw/Raw History storage lifecycle.

    S3 target settings are configurable so consumers can override DMS defaults
    without introducing Data Hub-specific assumptions into the reusable module.
  EOT

  type = object({
    endpoint_id             = string
    bucket_name             = string
    service_access_role_arn = optional(string)

    bucket_folder = optional(string)

    add_column_name        = optional(bool, true)
    cdc_max_batch_interval = optional(number, 3600)
    cdc_min_file_size      = optional(number, 32000)
    cdc_path               = optional(string)
    max_file_size          = optional(number)

    compression_type = optional(string, "GZIP")
    data_format      = optional(string, "parquet")
    encoding_type    = optional(string, "rle-dictionary")

    encryption_mode                    = optional(string, "SSE_S3")
    server_side_encryption_kms_key_arn = optional(string)

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
    condition = (
      var.s3_target_endpoint.service_access_role_arn == null
      ||
      length(trimspace(var.s3_target_endpoint.service_access_role_arn)) > 0
    )

    error_message = "s3_target_endpoint.service_access_role_arn must be null or a non-empty string."
  }

  validation {
    condition = (
      var.s3_target_endpoint.encryption_mode != "SSE_KMS"
      ||
      try(
        length(trimspace(var.s3_target_endpoint.server_side_encryption_kms_key_arn)) > 0,
        false
      )
    )

    error_message = "s3_target_endpoint.server_side_encryption_kms_key_arn must be supplied when encryption_mode is 'SSE_KMS'."
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
      var.s3_target_endpoint.cdc_path == null
      ||
      length(trimspace(var.s3_target_endpoint.cdc_path)) > 0
    )

    error_message = "s3_target_endpoint.cdc_path must be null or a non-empty string."
  }

  validation {
    condition = (
      var.s3_target_endpoint.max_file_size == null
      ||
      (
        var.s3_target_endpoint.max_file_size >= 1
        &&
        var.s3_target_endpoint.max_file_size <= 1048576
      )
    )

    error_message = "s3_target_endpoint.max_file_size must be between 1 and 1048576 KB when supplied."
  }

  validation {
    condition = (
      var.s3_target_endpoint.encryption_mode == "SSE_KMS"
      ||
      var.s3_target_endpoint.server_side_encryption_kms_key_arn == null
    )

    error_message = "s3_target_endpoint.server_side_encryption_kms_key_arn must only be supplied when encryption_mode is 'SSE_KMS'."
  }
}

#----------------------------------------------------------------------
# Replication Task Variables
#----------------------------------------------------------------------

variable "task_logging" {
  description = <<-EOT
    CloudWatch logging configuration for DMS replication tasks.

    AWS DMS writes task logs to the log group:
    dms-tasks-<replication-instance-id>

    This module manages that log group and its retention when logging is enabled
    and at least one replication task is configured.

    The account-level dms-cloudwatch-logs-role remains an external prerequisite
    and is intentionally not created by this module.

    log_components maps DMS component identifiers to logging severity.
    LOGGER_SEVERITY_DEFAULT is recommended for normal operation.
  EOT

  type = object({
    enabled           = optional(bool, true)
    retention_in_days = optional(number, 30)
    kms_key_arn       = optional(string)

    log_components = optional(map(string), {
      METADATA_MANAGER = "LOGGER_SEVERITY_DEFAULT"
      SORTER           = "LOGGER_SEVERITY_DEFAULT"
      SOURCE_CAPTURE   = "LOGGER_SEVERITY_DEFAULT"
      SOURCE_UNLOAD    = "LOGGER_SEVERITY_DEFAULT"
      TABLES_MANAGER   = "LOGGER_SEVERITY_DEFAULT"
      TARGET_APPLY     = "LOGGER_SEVERITY_DEFAULT"
      TARGET_LOAD      = "LOGGER_SEVERITY_DEFAULT"
      TASK_MANAGER     = "LOGGER_SEVERITY_DEFAULT"
    })
  })

  default = {}

  validation {
    condition = contains([
      1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545,
      731, 1096, 1827, 2192, 2557, 2922, 3288, 3653
    ], var.task_logging.retention_in_days)

    error_message = "task_logging.retention_in_days must be a retention period supported by CloudWatch Logs."
  }

  validation {
    condition = (
      var.task_logging.kms_key_arn == null
      ? true
      : length(trimspace(var.task_logging.kms_key_arn)) > 0
    )

    error_message = "task_logging.kms_key_arn must be null or a non-empty ARN."
  }

  validation {
    condition = (
      !var.task_logging.enabled
      ||
      length(var.task_logging.log_components) > 0
    )

    error_message = "task_logging.log_components must contain at least one component when logging is enabled."
  }

  validation {
    condition = alltrue([
      for severity in values(var.task_logging.log_components) :
      contains([
        "LOGGER_SEVERITY_ERROR",
        "LOGGER_SEVERITY_WARNING",
        "LOGGER_SEVERITY_INFO",
        "LOGGER_SEVERITY_DEFAULT",
        "LOGGER_SEVERITY_DEBUG",
        "LOGGER_SEVERITY_DETAILED_DEBUG"
      ], severity)
    ])

    error_message = "task_logging.log_components values must contain a supported AWS DMS logging severity."
  }
}

variable "replication_tasks" {
  description = <<-EOT
    DMS replication tasks to provision against the replication infrastructure
    created by this module.

    Each task defines infrastructure configuration only. Runtime execution,
    including task start/stop, sequencing, CDC recovery positions, retries and
    replay, remains outside this module and is owned by orchestration.

    table_mappings must contain prepared DMS table-mapping JSON. The module does
    not generate mappings or understand domain-specific table-selection rules.

    replication_task_settings can be supplied where task-specific DMS settings
    are required.
  EOT

  type = map(object({
    replication_task_id = string
    migration_type      = string
    table_mappings      = string

    replication_task_settings = optional(string)

    tags = optional(map(string), {})
  }))

  default = {}

  validation {
    condition = alltrue([
      for task in values(var.replication_tasks) :
      length(trimspace(task.replication_task_id)) > 0
    ])

    error_message = "replication_tasks replication_task_id values must not be empty."
  }

  validation {
    condition = alltrue([
      for task in values(var.replication_tasks) :
      contains(
        ["full-load", "cdc", "full-load-and-cdc"],
        task.migration_type
      )
    ])

    error_message = "replication_tasks migration_type must be one of: full-load, cdc, full-load-and-cdc."
  }

  validation {
    condition = alltrue([
      for task in values(var.replication_tasks) :
      can(jsondecode(task.table_mappings))
    ])

    error_message = "replication_tasks table_mappings must contain valid JSON."
  }

  validation {
    condition = alltrue([
      for task in values(var.replication_tasks) :
      task.replication_task_settings == null
      ||
      can(jsondecode(task.replication_task_settings))
    ])

    error_message = "replication_tasks replication_task_settings must be null or contain valid JSON."
  }

  validation {
    condition = alltrue([
      for task in values(var.replication_tasks) :
      task.replication_task_settings == null
      ||
      can(keys(jsondecode(task.replication_task_settings)))
    ])

    error_message = "replication_tasks replication_task_settings must be null or contain a JSON object."
  }

  validation {
    condition = alltrue([
      for task in values(var.replication_tasks) :
      task.replication_task_settings == null
      ||
      try(
        !contains(
          keys(jsondecode(task.replication_task_settings)),
          "Logging"
        ),
        false
      )
    ])

    error_message = "Configure DMS task logging through task_logging; replication_task_settings must not contain a top-level Logging property."
  }
}
