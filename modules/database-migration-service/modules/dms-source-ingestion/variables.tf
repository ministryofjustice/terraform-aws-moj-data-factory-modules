variable "name" {
  description = "Stable name used to identify this DMS source ingestion deployment."
  type        = string

  validation {
    condition     = length(trimspace(var.name)) > 0
    error_message = "name must be a non-empty string."
  }
}

variable "environment" {
  description = "Environment in which the DMS source ingestion resources are deployed."
  type        = string

  validation {
    condition     = length(trimspace(var.environment)) > 0
    error_message = "environment must be a non-empty string."
  }
}

variable "vpc_id" {
  description = "VPC in which the DMS replication infrastructure is deployed."
  type        = string

  validation {
    condition     = length(trimspace(var.vpc_id)) > 0
    error_message = "vpc_id must be a non-empty string."
  }
}

variable "tags" {
  description = "Tags applied to resources created for this source ingestion deployment."
  type        = map(string)
  default     = {}
}

variable "network" {
  description = <<-EOT
    Network configuration for the DMS replication infrastructure.
    Exactly one replication subnet strategy must be used:

      - provide at least two subnet_ids and allow the underlying DMS module to
        create the replication subnet group
    or
      - provide existing_replication_subnet_group_id to reuse an existing DMS
        replication subnet group.

    additional_security_group_ids can be used to attach externally managed
    security groups required for connectivity between DMS and the source.
  EOT

  type = object({
    subnet_ids                           = optional(list(string))
    existing_replication_subnet_group_id = optional(string)
    replication_subnet_group_name        = optional(string)

    additional_security_group_ids = optional(list(string), [])
    allow_all_egress              = optional(bool, true)
  })

  validation {
    condition = (
      (
        var.network.existing_replication_subnet_group_id != null
        &&
        try(length(var.network.subnet_ids), 0) == 0
      )
      ||
      (
        var.network.existing_replication_subnet_group_id == null
        &&
        try(length(var.network.subnet_ids), 0) >= 2
      )
    )

    error_message = "network must provide either existing_replication_subnet_group_id or at least two subnet_ids but not both."
  }

  validation {
    condition = (
      var.network.existing_replication_subnet_group_id == null
      ||
      length(trimspace(var.network.existing_replication_subnet_group_id)) > 0
    )

    error_message = "network.existing_replication_subnet_group_id must be null or a non-empty string."
  }

  validation {
    condition = (
      var.network.replication_subnet_group_name == null
      ||
      (
        var.network.existing_replication_subnet_group_id == null
        &&
        length(trimspace(var.network.replication_subnet_group_name)) > 0
      )
    )

    error_message = "network.replication_subnet_group_name may only be supplied when the module creates the replication subnet group."
  }

  validation {
    condition = alltrue([
      for subnet_id in coalesce(var.network.subnet_ids, []) :
      length(trimspace(subnet_id)) > 0
    ])

    error_message = "network.subnet_ids must not contain empty subnet IDs."
  }

  validation {
    condition = alltrue([
      for security_group_id in var.network.additional_security_group_ids :
      length(trimspace(security_group_id)) > 0
    ])

    error_message = "network.additional_security_group_ids must not contain empty security group IDs."
  }
}

variable "replication_instance" {
  description = "Configuration and optional overrides for the DMS replication instance."

  type = object({
    instance_class    = string
    allocated_storage = number

    engine_version = optional(string)
    kms_key_arn    = optional(string)

    multi_az          = optional(bool, false)
    availability_zone = optional(string)

    apply_immediately            = optional(bool, false)
    auto_minor_version_upgrade   = optional(bool, false)
    preferred_maintenance_window = optional(string, "sun:10:30-sun:14:30")
  })

  validation {
    condition     = length(trimspace(var.replication_instance.instance_class)) > 0
    error_message = "replication_instance.instance_class must be a non-empty string."
  }

  validation {
    condition     = var.replication_instance.allocated_storage > 0
    error_message = "replication_instance.allocated_storage must be greater than zero."
  }

  validation {
    condition = (
      var.replication_instance.engine_version == null
      ||
      length(trimspace(var.replication_instance.engine_version)) > 0
    )

    error_message = "replication_instance.engine_version must be null or a non-empty string."
  }

  validation {
    condition = (
      var.replication_instance.kms_key_arn == null
      ||
      length(trimspace(var.replication_instance.kms_key_arn)) > 0
    )

    error_message = "replication_instance.kms_key_arn must be null or a non-empty string."
  }

  validation {
    condition = (
      var.replication_instance.availability_zone == null
      ||
      length(trimspace(var.replication_instance.availability_zone)) > 0
    )

    error_message = "replication_instance.availability_zone must be null or a non-empty string."
  }

  validation {
    condition = !(
      var.replication_instance.multi_az
      &&
      var.replication_instance.availability_zone != null
    )

    error_message = "replication_instance.availability_zone must not be supplied when multi_az is true."
  }
}

variable "source_endpoint" {
  description = <<-EOT
    Source db configuration.

    PostgreSQL and Oracle are currently supported.

    Authentication is provided through AWS Secrets Manager. Terraform passes
    secret references to DMS and does not read or decode credential values.

    postgres may only be supplied for PostgreSQL sources.

    oracle may only be supplied for Oracle sources. Oracle ASM credentials are
    represented by a separate Secrets Manager reference where Binary Reader
    requires ASM access.
  EOT

  type = object({
    engine        = string
    database_name = string

    secret_arn             = string
    secret_access_role_arn = optional(string)
    secret_kms_key_arn     = optional(string)

    ssl_mode        = optional(string, "none")
    certificate_arn = optional(string)

    extra_connection_attributes = optional(string)

    postgres = optional(object({
      map_boolean_as_boolean       = optional(bool, true)
      fail_tasks_on_lob_truncation = optional(bool, true)
      heartbeat_enable             = optional(bool, true)
      heartbeat_frequency          = optional(number, 5)
    }))

    oracle = optional(object({
      asm_secret_arn  = optional(string)
      asm_kms_key_arn = optional(string)
    }))
  })

  validation {
    condition     = contains(["oracle", "postgres"], var.source_endpoint.engine)
    error_message = "source_endpoint.engine must be either 'oracle' or 'postgres'."
  }

  validation {
    condition     = length(trimspace(var.source_endpoint.database_name)) > 0
    error_message = "source_endpoint.database_name must be a non-empty string."
  }

  validation {
    condition     = length(trimspace(var.source_endpoint.secret_arn)) > 0
    error_message = "source_endpoint.secret_arn must be a non-empty string."
  }

  validation {
    condition = (
      var.source_endpoint.secret_access_role_arn == null
      ||
      length(trimspace(var.source_endpoint.secret_access_role_arn)) > 0
    )

    error_message = "source_endpoint.secret_access_role_arn must be null or a non-empty string."
  }

  validation {
    condition = (
      var.source_endpoint.secret_kms_key_arn == null
      ||
      length(trimspace(var.source_endpoint.secret_kms_key_arn)) > 0
    )

    error_message = "source_endpoint.secret_kms_key_arn must be null or a non-empty string."
  }

  validation {
    condition = contains(
      ["none", "require", "verify-ca", "verify-full"],
      var.source_endpoint.ssl_mode
    )

    error_message = "source_endpoint.ssl_mode must be one of: none, require, verify-ca, verify-full."
  }

  validation {
    condition = (
      !contains(["verify-ca", "verify-full"], var.source_endpoint.ssl_mode)
      ||
      (
        var.source_endpoint.certificate_arn != null
        &&
        length(trimspace(var.source_endpoint.certificate_arn)) > 0
      )
    )

    error_message = "source_endpoint.certificate_arn must be supplied when source_endpoint.ssl_mode is 'verify-ca' or 'verify-full'."
  }

  validation {
    condition = (
      var.source_endpoint.postgres == null
      ||
      var.source_endpoint.engine == "postgres"
    )

    error_message = "source_endpoint.postgres may only be supplied when source_endpoint.engine is 'postgres'."
  }

  validation {
    condition = (
      var.source_endpoint.postgres == null
      ||
      var.source_endpoint.postgres.heartbeat_frequency > 0
    )

    error_message = "source_endpoint.postgres.heartbeat_frequency must be greater than zero."
  }

  validation {
    condition = (
      var.source_endpoint.oracle == null
      ||
      var.source_endpoint.engine == "oracle"
    )

    error_message = "source_endpoint.oracle may only be supplied when source_endpoint.engine is 'oracle'."
  }

  validation {
    condition = (
      var.source_endpoint.oracle == null
      ||
      var.source_endpoint.oracle.asm_secret_arn == null
      ||
      length(trimspace(var.source_endpoint.oracle.asm_secret_arn)) > 0
    )

    error_message = "source_endpoint.oracle.asm_secret_arn must be null or a non-empty string."
  }

  validation {
    condition = (
      var.source_endpoint.oracle == null
      ||
      var.source_endpoint.oracle.asm_kms_key_arn == null
      ||
      (
        var.source_endpoint.oracle.asm_secret_arn != null
        &&
        length(trimspace(var.source_endpoint.oracle.asm_secret_arn)) > 0
      )
    )

    error_message = "source_endpoint.oracle.asm_secret_arn must be supplied when source_endpoint.oracle.asm_kms_key_arn is configured."
  }

  validation {
    condition = (
      var.source_endpoint.oracle == null
      ||
      var.source_endpoint.oracle.asm_kms_key_arn == null
      ||
      length(trimspace(var.source_endpoint.oracle.asm_kms_key_arn)) > 0
    )

    error_message = "source_endpoint.oracle.asm_kms_key_arn must be null or a non-empty string."
  }
}

variable "target" {
  description = <<-EOT
    S3 target configuration for the ingestion deployment.

    The adapter owns common ingestion-format conventions. Consumers provide the
    target location, security configuration and source-specific CDC sizing
    overrides where required.
  EOT

  type = object({
    bucket_name   = string
    bucket_folder = optional(string)

    service_access_role_arn = optional(string)

    encryption = optional(object({
      mode        = optional(string, "SSE_S3")
      kms_key_arn = optional(string)
    }), {})

    cdc = optional(object({
      path               = optional(string, "cdc")
      max_batch_interval = optional(number, 10)
      min_file_size      = optional(number, 32000)
      max_file_size      = optional(number, 120000)
    }), {})
  })

  validation {
    condition     = length(trimspace(var.target.bucket_name)) > 0
    error_message = "target.bucket_name must be a non-empty string."
  }

  validation {
    condition = (
      var.target.bucket_folder == null
      ||
      length(trimspace(var.target.bucket_folder)) > 0
    )

    error_message = "target.bucket_folder must be null or a non-empty string."
  }

  validation {
    condition = (
      var.target.service_access_role_arn == null
      ||
      length(trimspace(var.target.service_access_role_arn)) > 0
    )

    error_message = "target.service_access_role_arn must be null or a non-empty string."
  }

  validation {
    condition = contains(
      ["SSE_S3", "SSE_KMS"],
      var.target.encryption.mode
    )

    error_message = "target.encryption.mode must be either 'SSE_S3' or 'SSE_KMS'."
  }

  validation {
    condition = (
      var.target.encryption.mode != "SSE_KMS"
      ||
      (
        var.target.encryption.kms_key_arn != null
        &&
        length(trimspace(var.target.encryption.kms_key_arn)) > 0
      )
    )

    error_message = "target.encryption.kms_key_arn must be supplied when target.encryption.mode is 'SSE_KMS'."
  }

  validation {
    condition = (
      var.target.encryption.mode == "SSE_KMS"
      ||
      var.target.encryption.kms_key_arn == null
    )

    error_message = "target.encryption.kms_key_arn may only be supplied when target.encryption.mode is 'SSE_KMS'."
  }

  validation {
    condition     = length(trimspace(var.target.cdc.path)) > 0
    error_message = "target.cdc.path must be a non-empty string."
  }

  validation {
    condition     = var.target.cdc.max_batch_interval > 0
    error_message = "target.cdc.max_batch_interval must be greater than zero."
  }

  validation {
    condition     = var.target.cdc.min_file_size > 0
    error_message = "target.cdc.min_file_size must be greater than zero."
  }

  validation {
    condition     = var.target.cdc.max_file_size >= 1 && var.target.cdc.max_file_size <= 1048576
    error_message = "target.cdc.max_file_size must be between 1 and 1048576 KB."
  }
}

variable "monitoring" {
  description = "CloudWatch monitoring configuration for the DMS replication infrastructure."

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

variable "task_logging" {
  description = <<-EOT
    CloudWatch logging configuration for DMS replication tasks.

    Logging is enabled by default with 30-day retention and operational DMS
    components set to LOGGER_SEVERITY_DEFAULT.

    The account-level dms-cloudwatch-logs-role must already exist.
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
    DMS replication task definitions for this ingestion deployment.

    The adapter provisions task infrastructure only. Runtime start/stop,
    sequencing, recovery positions, retries and replay remain the responsibility
    of the orchestration layer.
  EOT

  type = map(object({
    replication_task_id       = string
    migration_type            = string
    table_mappings            = string
    replication_task_settings = optional(string)
    tags                      = optional(map(string), {})
  }))

  default = {}

  validation {
    condition = alltrue([
      for task in values(var.replication_tasks) :
      contains(["full-load", "cdc", "full-load-and-cdc"], task.migration_type)
    ])

    error_message = "replication_tasks migration_type must be one of: full-load, cdc, full-load-and-cdc."
  }

  validation {
    condition = alltrue([
      for task in values(var.replication_tasks) :
      length(trimspace(task.replication_task_id)) > 0
    ])

    error_message = "replication_tasks replication_task_id must not be empty."
  }

  validation {
    condition = alltrue([
      for task in values(var.replication_tasks) :
      length(trimspace(task.table_mappings)) > 0
    ])

    error_message = "replication_tasks table_mappings must not be empty."
  }
}
