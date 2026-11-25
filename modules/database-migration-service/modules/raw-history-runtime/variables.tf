variable "function_name" {
  description = "Name of the Raw History receiver Lambda."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{1,64}$", var.function_name))
    error_message = "function_name must contain 1–64 letters, numbers, underscores or hyphens."
  }
}

variable "service_context" {
  description = "Service that this Raw History receiver is authorised to handle."

  type = object({
    environment   = string
    account_id    = string
    region        = string
    source        = string
    database_name = string
  })

  nullable = false

  validation {
    condition = alltrue([
      for value in values(var.service_context) :
      try(
        length(trimspace(value)) > 0 &&
        value == trimspace(value),
        false
      )
    ]) && can(regex("^[0-9]{12}$", var.service_context.account_id))

    error_message = "service_context must contain trimmed, non-empty values and a 12-digit account ID."
  }
}

variable "registry" {
  description = "Existing shared Ingestion Registry."

  type = object({
    table_name = string
    table_arn  = string
  })

  nullable = false

  validation {
    condition = (
      can(regex("^[A-Za-z0-9_.-]{3,255}$", var.registry.table_name)) &&
      try(
        length(trimspace(var.registry.table_arn)) > 0 &&
        var.registry.table_arn == trimspace(var.registry.table_arn),
        false
      )
    )

    error_message = "registry must identify an existing Ingestion Registry table."
  }
}

variable "configuration_store" {
  description = "Existing versioned bucket and prefix for immutable receiver configuration."

  type = object({
    bucket_name  = string
    bucket_owner = string
    prefix       = string
    kms_key_arn  = string
  })

  nullable = false
}

variable "published_documents" {
  description = "Authorised location of published service configuration and component catalogs."

  type = object({
    bucket_name  = string
    bucket_owner = string
    prefix       = string
    kms_key_arns = optional(set(string), [])
  })

  nullable = false
}

variable "history_bucket" {
  description = "Existing destination bucket for Raw History objects and commit evidence."

  type = object({
    bucket_name  = string
    bucket_owner = string
    kms_key_arn  = string
  })

  nullable = false
}

variable "operations_kms_key_arn" {
  description = "KMS key for the receiver's operation table."
  type        = string
  nullable    = false
}

variable "tables" {
  description = "Canonical history destinations and authorised input locations for each table."

  type = list(object({
    schema_name    = string
    table_name     = string
    profile        = string
    history_prefix = string

    landings = list(object({
      bucket_name  = string
      bucket_owner = string
      prefix       = string
      kms_key_arns = optional(set(string), [])
    }))

    manifest_location = object({
      bucket_name  = string
      bucket_owner = string
      prefix       = string
      kms_key_arns = optional(set(string), [])
    })

    contract_location = object({
      bucket_name  = string
      bucket_owner = string
      prefix       = string
      kms_key_arns = optional(set(string), [])
    })
  }))

  nullable = false

  validation {
    condition = try(
      length(var.tables) > 0 &&
      alltrue([
        for table in var.tables :
        can(regex("^[A-Za-z_][A-Za-z0-9_$#]{0,127}$", table.schema_name)) &&
        can(regex("^[A-Za-z_][A-Za-z0-9_$#]{0,127}$", table.table_name)) &&
        length(trimspace(table.profile)) > 0 &&
        table.profile == trimspace(table.profile) &&
        length(table.landings) > 0
      ]),
      false
    )

    error_message = "Configure at least one table with valid database identifiers, a history profile and at least one Raw landing."
  }
}

variable "max_batch_bytes" {
  description = "Maximum total Raw input bytes accepted by the bounded receiver."
  type        = number
  default     = 8 * 1024 * 1024
  nullable    = false

  validation {
    condition = (
      var.max_batch_bytes == floor(var.max_batch_bytes) &&
      var.max_batch_bytes >= 1 &&
      var.max_batch_bytes <= 64 * 1024 * 1024
    )

    error_message = "max_batch_bytes must be an integer from 1 byte to 64 MiB."
  }
}

variable "deletion_protection_enabled" {
  description = "Protect the operation table against accidental deletion."
  type        = bool
  default     = true
  nullable    = false
}

variable "tags" {
  description = "Tags applied to resources created by this module."
  type        = map(string)
  default     = {}
  nullable    = false
}

variable "artifact" {
  description = "Versioned S3 object containing the tested runtime Lambda ZIP."

  type = object({
    bucket_name      = string
    object_key       = string
    version_id       = string
    source_code_hash = string
  })

  nullable = false

  validation {
    condition = alltrue([
      for value in values(var.artifact) :
      try(
        length(trimspace(value)) > 0 &&
        value == trimspace(value),
        false
      )
      ]) && var.artifact.version_id != "null" && can(regex(
      "^[A-Za-z0-9+/]{43}=$",
      var.artifact.source_code_hash
    ))

    error_message = "artifact must identify a versioned S3 object and include a base64-encoded SHA-256 checksum."
  }
}

variable "registry_kms_key_arn" {
  description = "KMS key for the shared Ingestion Registry, where required."
  type        = string
  default     = null
}

variable "log_retention_days" {
  description = "CloudWatch log retention period."
  type        = number
  default     = 30
  nullable    = false
}
