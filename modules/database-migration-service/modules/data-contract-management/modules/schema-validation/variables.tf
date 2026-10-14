variable "name" {
  description = "Unique resource name supplied by the caller, including environment naming."
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

variable "ingestion_type" {
  description = "Ingestion mode recorded in validation results. Both modes validate each incoming object."
  type        = string
  nullable    = false

  validation {
    condition     = contains(["FULL", "CDC"], var.ingestion_type)
    error_message = "ingestion_type must be FULL or CDC."
  }
}

variable "source_mappings" {
  description = "Incoming bucket/prefix locations mapped to stable contract identities. Each mapping identifies one table."
  type = list(object({
    bucket_name   = string
    prefix        = string
    source        = string
    database_name = string
    schema_name   = string
    table_name    = string
  }))
  nullable = false

  validation {
    condition = (
      length(var.source_mappings) > 0 &&
      alltrue([
        for mapping in var.source_mappings :
        alltrue([
          for value in [
            mapping.source,
            mapping.database_name,
            mapping.schema_name,
            mapping.table_name,
          ] :
          length(trimspace(value)) > 0 &&
          value == trimspace(value) &&
          length(regexall(":", value)) == 0
        ])
      ])
    )
    error_message = "Supply at least one mapping. Identity values must be non-empty, have no surrounding whitespace and contain no colon."
  }

  validation {
    condition = alltrue([
      for mapping in var.source_mappings :
      length(trimspace(mapping.bucket_name)) > 0 &&
      mapping.bucket_name == trimspace(mapping.bucket_name) &&
      length(regexall("[/:*?]", mapping.bucket_name)) == 0
    ])
    error_message = "Mapping bucket_name must be a bucket name, without surrounding whitespace, URI separators or wildcards."
  }

  validation {
    condition = alltrue([
      for mapping in var.source_mappings :
      length(mapping.prefix) > 0 &&
      mapping.prefix == trimspace(mapping.prefix) &&
      substr(mapping.prefix, 0, 1) != "/" &&
      substr(mapping.prefix, max(0, length(mapping.prefix) - 1), 1) == "/" &&
      length(regexall("[*?]", mapping.prefix)) == 0
    ])
    error_message = "Mapping prefixes must be non-empty, have no surrounding whitespace or wildcards, have no leading slash and end with a slash."
  }

  validation {
    condition = alltrue([
      for mapping in var.source_mappings :
      can(regex("^[A-Za-z_][A-Za-z0-9_]*$", mapping.table_name))
    ])
    error_message = "Mapping table_name must follow the current Avro naming policy."
  }

  validation {
    condition = alltrue(flatten([
      for left_index, left in var.source_mappings : [
        for right_index, right in var.source_mappings :
        left_index >= right_index ||
        left.bucket_name != right.bucket_name ||
        !(
          substr(left.prefix, 0, min(length(left.prefix), length(right.prefix))) ==
          substr(right.prefix, 0, min(length(left.prefix), length(right.prefix)))
        )
      ]
    ]))
    error_message = "Mappings in the same bucket must not have duplicate or overlapping prefixes."
  }
}

variable "registry_table_name" {
  description = "Existing contract registry table name. Validation reads this table."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]{3,255}$", var.registry_table_name))
    error_message = "registry_table_name must be a valid DynamoDB table name."
  }
}

variable "registry_table_arn" {
  description = "ARN of the existing contract registry table, used to scope read permissions."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^arn:[^:]+:dynamodb:[^:]+:[0-9]{12}:table/[A-Za-z0-9_.-]+$", var.registry_table_arn))
    error_message = "registry_table_arn must be a DynamoDB table ARN, not an index ARN or wildcard."
  }
}

variable "registry_kms_key_arns" {
  description = "Customer-managed KMS keys used by the existing contract registry."
  type        = set(string)
  default     = []
  nullable    = false
}

variable "contract_object_arns" {
  description = "Registered contract S3 object ARNs or prefix patterns that validation may read."
  type        = set(string)
  nullable    = false

  validation {
    condition = length(var.contract_object_arns) > 0 && alltrue([
      for arn in var.contract_object_arns :
      can(regex("^arn:[^:]+:s3:::[^/*]+/.+$", arn))
    ])
    error_message = "Supply at least one contract S3 object ARN or object-prefix pattern."
  }
}

variable "contract_kms_key_arns" {
  description = "Customer-managed KMS keys used by registered contract objects."
  type        = set(string)
  default     = []
  nullable    = false
}

variable "incoming_kms_key_arns" {
  description = "Customer-managed KMS keys used by incoming Parquet objects."
  type        = set(string)
  default     = []
  nullable    = false
}

variable "validation_table_kms_key_arn" {
  description = "Customer-managed KMS key for the validation audit table."
  type        = string
  nullable    = false
}

variable "table_deletion_protection_enabled" {
  description = "Protect the validation audit table against deletion."
  type        = bool
  default     = true
  nullable    = false
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
  description = "Maximum duration of one validation invocation."
  type        = number
  default     = 120
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
  description = "Lambda memory allocation for Parquet schema reading and comparison."
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
  description = "Validation concurrency limit. -1 uses shared concurrency; 0 pauses execution."
  type        = number
  default     = 5
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
