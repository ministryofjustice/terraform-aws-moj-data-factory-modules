variable "name" {
  description = "Unique resource name supplied by the caller, including environment and source naming."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[a-zA-Z0-9][a-zA-Z0-9_-]{2,44}$", var.name))
    error_message = "name must be 3–45 characters using letters, numbers, underscores or hyphens."
  }
}

variable "image_uri" {
  description = "Published ECR image URI pinned to a SHA-256 digest. The image must match architecture."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[^\\s]+@sha256:[a-f0-9]{64}$", var.image_uri))
    error_message = "image_uri must reference an image digest, not a mutable tag."
  }
}

variable "architecture" {
  description = "Architecture of the published Lambda image."
  type        = string
  default     = "arm64"
  nullable    = false

  validation {
    condition     = contains(["arm64", "x86_64"], var.architecture)
    error_message = "architecture must be arm64 or x86_64."
  }
}

variable "source_config" {
  description = "Source configuration. Credentials remain in Secrets Manager; service is the stable contract source identity."
  type = object({
    engine              = string
    service             = string
    database_name       = string
    schema_name         = string
    table_names         = list(string)
    secret_arn          = string
    oracle_service_name = optional(string)
    oracle_sid          = optional(string)
  })
  nullable = false

  validation {
    condition     = contains(["postgres", "oracle"], var.source_config.engine)
    error_message = "source_config.engine must be postgres or oracle."
  }

  validation {
    condition = alltrue([
      for value in [
        var.source_config.service,
        var.source_config.database_name,
        var.source_config.schema_name,
      ] :
      length(trimspace(value)) > 0 &&
      value == trimspace(value) &&
      length(regexall(":", value)) == 0
    ])
    error_message = "Source identity values must be non-empty, have no surrounding whitespace and contain no colon."
  }

  validation {
    condition = (
      length(var.source_config.table_names) > 0 &&
      length(distinct(var.source_config.table_names)) == length(var.source_config.table_names) &&
      alltrue([
        for name in var.source_config.table_names :
        can(regex("^[A-Za-z_][A-Za-z0-9_]*$", name))
      ])
    )
    error_message = "Supply a non-empty list of unique table names supported by the current Avro naming policy: letters, numbers and underscores, starting with a letter or underscore."
  }

  validation {
    condition     = can(regex("^arn:[^:]+:secretsmanager:[^:]+:[0-9]{12}:secret:.+$", var.source_config.secret_arn))
    error_message = "source_config.secret_arn must be a Secrets Manager secret ARN."
  }

  validation {
    condition = (
      var.source_config.engine == "oracle" ?
      (
        (var.source_config.oracle_service_name == null) !=
        (var.source_config.oracle_sid == null)
      ) :
      (
        var.source_config.oracle_service_name == null &&
        var.source_config.oracle_sid == null
      )
    )
    error_message = "Oracle requires exactly one of oracle_service_name or oracle_sid. PostgreSQL requires neither."
  }

  validation {
    condition = alltrue([
      for value in [
        var.source_config.oracle_service_name,
        var.source_config.oracle_sid,
      ] :
      value == null ? true : (
        length(trimspace(value)) > 0 &&
        value == trimspace(value)
      )
    ])
    error_message = "Oracle connection identifiers must be non-empty and have no surrounding whitespace when supplied."
  }
}

variable "contract_namespace" {
  description = "Avro namespace for generated contracts."
  type        = string
  nullable    = false

  validation {
    condition = can(
      regex(
        "^[A-Za-z_][A-Za-z0-9_]*([.][A-Za-z_][A-Za-z0-9_]*)*$",
        var.contract_namespace
      )
    )
    error_message = "contract_namespace must contain valid Avro identifiers separated by dots."
  }
}

variable "contract_version" {
  description = "Version supplied by the controlled generation process. Generation does not allocate versions."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^v[1-9][0-9]*$", var.contract_version))
    error_message = "Use v1 onwards, with lowercase v and no leading zeros. v0 is reserved for future migration."
  }
}

variable "contract_contacts" {
  description = "Optional contact metadata for the source service."
  type        = map(string)
  default     = {}
  nullable    = false

  validation {
    condition = alltrue([
      for key, value in var.contract_contacts :
      length(trimspace(key)) > 0 &&
      key == trimspace(key) &&
      (
        value == null ? false : (
          length(trimspace(value)) > 0 &&
          value == trimspace(value)
        )
      )
    ])
    error_message = "Contact names and values must be non-empty and have no surrounding whitespace."
  }
}

variable "schema_registry_bucket_name" {
  description = "Existing S3 bucket where generated contracts are published."
  type        = string
  nullable    = false

  validation {
    condition = (
      length(trimspace(var.schema_registry_bucket_name)) > 0 &&
      var.schema_registry_bucket_name == trimspace(var.schema_registry_bucket_name) &&
      length(regexall("[/:*]", var.schema_registry_bucket_name)) == 0
    )
    error_message = "Supply a bucket name, not an ARN, URI or wildcard."
  }
}

variable "contract_object_arns" {
  description = "S3 object ARNs or prefix patterns the function may write and read for duplicate comparison."
  type        = set(string)
  nullable    = false

  validation {
    condition = length(var.contract_object_arns) > 0 && alltrue([
      for arn in var.contract_object_arns :
      can(regex("^arn:[^:]+:s3:::[^/*]+/.+$", arn))
    ])
    error_message = "Supply at least one S3 object ARN or object-prefix pattern."
  }
}

variable "contract_kms_key_arns" {
  description = "Customer-managed KMS keys used by the configured contract storage."
  type        = set(string)
  default     = []
  nullable    = false
}

variable "secret_kms_key_arns" {
  description = "Customer-managed KMS keys used by the database secret. Empty when the secret uses an AWS-managed key."
  type        = set(string)
  default     = []
  nullable    = false
}

variable "network" {
  description = "Existing private subnets and security groups for source database and AWS API connectivity. The caller owns routing and security-group rules."
  type = object({
    subnet_ids         = set(string)
    security_group_ids = set(string)
  })
  nullable = false

  validation {
    condition = (
      length(var.network.subnet_ids) >= 2 &&
      length(var.network.subnet_ids) <= 16 &&
      alltrue([
        for id in var.network.subnet_ids :
        can(regex("^subnet-[a-f0-9]+$", id))
      ])
    )
    error_message = "Supply 2–16 subnet IDs. Select subnets across availability zones in the same VPC."
  }

  validation {
    condition = (
      length(var.network.security_group_ids) >= 1 &&
      length(var.network.security_group_ids) <= 5 &&
      alltrue([
        for id in var.network.security_group_ids :
        can(regex("^sg-[a-f0-9]+$", id))
      ])
    )
    error_message = "Supply 1–5 security-group IDs from the selected VPC."
  }
}

variable "log_kms_key_arn" {
  description = "Customer-managed key for CloudWatch Logs. Its policy must allow the regional Logs service."
  type        = string
  nullable    = false
}

variable "failure_queue_kms_key_arn" {
  description = "Optional customer-managed key for the failure queue. Null uses SQS-managed encryption."
  type        = string
  default     = null
}

variable "lambda_environment_kms_key_arn" {
  description = "Optional customer-managed key for Lambda environment encryption."
  type        = string
  default     = null
}

variable "permissions_boundary_arn" {
  description = "Optional permissions boundary for the execution role."
  type        = string
  default     = null
}

variable "timeout_seconds" {
  description = "Maximum duration of one generation invocation."
  type        = number
  default     = 300
  nullable    = false

  validation {
    condition = (
      var.timeout_seconds >= 1 &&
      var.timeout_seconds <= 900 &&
      floor(var.timeout_seconds) == var.timeout_seconds
    )
    error_message = "timeout_seconds must be an integer between 1 and 900."
  }
}

variable "memory_size_mb" {
  description = "Lambda memory allocation."
  type        = number
  default     = 512
  nullable    = false

  validation {
    condition = (
      var.memory_size_mb >= 128 &&
      var.memory_size_mb <= 10240 &&
      floor(var.memory_size_mb) == var.memory_size_mb
    )
    error_message = "memory_size_mb must be an integer between 128 and 10240."
  }
}

variable "reserved_concurrent_executions" {
  description = "Concurrency limit controlling simultaneous source database connections. -1 uses shared concurrency; 0 pauses execution."
  type        = number
  default     = 2
  nullable    = false

  validation {
    condition = (
      var.reserved_concurrent_executions >= -1 &&
      floor(var.reserved_concurrent_executions) == var.reserved_concurrent_executions
    )
    error_message = "reserved_concurrent_executions must be an integer of -1 or greater."
  }
}

variable "log_retention_in_days" {
  description = "CloudWatch log retention."
  type        = number
  default     = 365
  nullable    = false

  validation {
    condition = contains(
      [1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653],
      var.log_retention_in_days
    )
    error_message = "Choose a supported CloudWatch log retention period."
  }
}

variable "maximum_retry_attempts" {
  description = "Retries for failed asynchronous invocations."
  type        = number
  default     = 2
  nullable    = false

  validation {
    condition     = contains([0, 1, 2], var.maximum_retry_attempts)
    error_message = "maximum_retry_attempts must be 0, 1 or 2."
  }
}

variable "maximum_event_age_in_seconds" {
  description = "Maximum age of an asynchronous invocation before expiry."
  type        = number
  default     = 3600
  nullable    = false

  validation {
    condition = (
      var.maximum_event_age_in_seconds >= 60 &&
      var.maximum_event_age_in_seconds <= 21600 &&
      floor(var.maximum_event_age_in_seconds) == var.maximum_event_age_in_seconds
    )
    error_message = "maximum_event_age_in_seconds must be an integer between 60 and 21600."
  }
}

variable "monitoring_enabled" {
  description = "Create operational alarms."
  type        = bool
  default     = true
  nullable    = false
}

variable "alarm_action_arns" {
  description = "Alarm action ARNs supplied by the caller. Empty creates alarms without notifications."
  type        = list(string)
  default     = []
  nullable    = false
}

variable "tags" {
  description = "Resource tags supplied by the caller."
  type        = map(string)
  default     = {}
  nullable    = false
}
