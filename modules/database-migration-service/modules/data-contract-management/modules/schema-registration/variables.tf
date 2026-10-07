variable "name" {
  description = "Unique resource name supplied by the consumer, including any environment naming."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z0-9][a-zA-Z0-9_-]{2,44}$", var.name))
    error_message = "name must be 3–45 characters using letters, numbers, underscores or hyphens."
  }
}

variable "image_uri" {
  description = "ECR image URI pinned to a SHA-256 digest. The image must match architecture."
  type        = string

  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.image_uri))
    error_message = "image_uri must reference an image digest, not a mutable tag."
  }
}

variable "architecture" {
  description = "Architecture of the published Lambda image."
  type        = string
  default     = "arm64"

  validation {
    condition     = contains(["arm64", "x86_64"], var.architecture)
    error_message = "architecture must be arm64 or x86_64."
  }
}

variable "contract_object_arns" {
  description = "S3 object ARNs or prefix patterns the Lambda may read, for example arn:aws:s3:::bucket/validated/*."
  type        = set(string)

  validation {
    condition = length(var.contract_object_arns) > 0 && alltrue([
      for arn in var.contract_object_arns :
      can(regex("^arn:[^:]+:s3:::[^/*]+/.+$", arn))
    ])
    error_message = "Supply at least one S3 object ARN or object-prefix pattern."
  }
}

variable "contract_kms_key_arns" {
  description = "Customer-managed KMS keys used to encrypt the allowed S3 contracts. Empty when unnecessary."
  type        = set(string)
  default     = []
}

variable "table_kms_key_arn" {
  description = "Customer-managed KMS key for the registration table."
  type        = string
}

variable "log_kms_key_arn" {
  description = "Customer-managed KMS key for CloudWatch Logs. Its policy must allow the regional Logs service."
  type        = string
}

variable "failure_queue_kms_key_arn" {
  description = "Optional customer-managed KMS key for the failure queue. Null uses SQS-managed encryption."
  type        = string
  default     = null
}

variable "lambda_environment_kms_key_arn" {
  description = "Optional customer-managed KMS key for Lambda environment variables."
  type        = string
  default     = null
}

variable "permissions_boundary_arn" {
  description = "Optional permissions boundary for the Lambda execution role."
  type        = string
  default     = null
}

variable "timeout_seconds" {
  description = "Maximum time allowed for one registration invocation."
  type        = number
  default     = 60

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
  default     = 256

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
  description = "Lambda concurrency limit. -1 uses shared account concurrency; 0 pauses execution."
  type        = number
  default     = 5

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

  validation {
    condition = contains(
      [1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653],
      var.log_retention_in_days
    )
    error_message = "Choose a supported CloudWatch log retention period."
  }
}

variable "table_deletion_protection_enabled" {
  description = "Protect the registration table from deletion. Disable explicitly before an intentional teardown."
  type        = bool
  default     = true
}

variable "maximum_retry_attempts" {
  description = "Retries for failed asynchronous Lambda invocations."
  type        = number
  default     = 2

  validation {
    condition     = contains([0, 1, 2], var.maximum_retry_attempts)
    error_message = "maximum_retry_attempts must be 0, 1 or 2."
  }
}

variable "maximum_event_age_in_seconds" {
  description = "Maximum age of an asynchronous event before it is sent to the failure destination."
  type        = number
  default     = 3600

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
}

variable "alarm_action_arns" {
  description = "Alarm notification/action ARNs supplied by the consumer. Empty creates alarms without notifications."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags supplied by the consumer."
  type        = map(string)
  default     = {}
}