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

variable "task_logging" {
  description = <<-EOT
    CloudWatch logging configuration for DMS replication tasks.

    AWS DMS writes task logs to the log group:
    dms-tasks-<replication-instance-id>

    This module manages the shared task log group and its retention when
    task_logging.enabled and create_task_log_group are both true.

    The account-level dms-cloudwatch-logs-role remains an external prerequisite
    and is intentionally not created by this module.

    The module configures whether task logging is enabled. AWS DMS retains its
    default logging components and LOGGER_SEVERITY_DEFAULT severities.
  EOT

  type = object({
    enabled           = optional(bool, true)
    retention_in_days = optional(number, 30)
    kms_key_arn       = optional(string)
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
}


variable "create_task_log_group" {
  description = "Whether this capacity owns a task log group."
  type        = bool
  default     = true
}
