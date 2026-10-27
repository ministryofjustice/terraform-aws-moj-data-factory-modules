variable "function_name" {
  description = "Name of the DMS runtime controller Lambda."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{1,64}$", var.function_name))
    error_message = "function_name must contain 1–64 letters, numbers, underscores or hyphens."
  }
}

variable "service_context" {
  description = "Service that this controller is authorised to operate."
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
      value != null && try(length(trimspace(value)) > 0 && value == trimspace(value), false)
    ]) && can(regex("^[0-9]{12}$", var.service_context.account_id))

    error_message = "service_context values must be non-empty trimmed strings and account_id must contain 12 digits."
  }
}

variable "artifact" {
  description = "Versioned S3 object containing the tested Lambda ZIP."
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
      value != null && try(length(trimspace(value)) > 0 && value == trimspace(value), false)
      ]) && var.artifact.version_id != "null" && can(regex(
      "^[A-Za-z0-9+/]{43}=$",
      var.artifact.source_code_hash
    ))

    error_message = "artifact must identify a versioned S3 object and include a base64-encoded SHA-256 checksum."
  }
}

variable "registry" {
  description = "Shared Ingestion Registry table used by the controller."
  type = object({
    table_name = string
    table_arn  = string
  })
  nullable = false

  validation {
    condition = can(regex(
      "^[A-Za-z0-9_.-]{3,255}$",
      var.registry.table_name
      )) && try(
      length(trimspace(var.registry.table_arn)) > 0 &&
      var.registry.table_arn == trimspace(var.registry.table_arn),
      false
    )

    error_message = "registry must include a valid table name and a non-empty trimmed table ARN."
  }
}

variable "published_documents" {
  description = "Authorised location of the service's published configuration documents."
  type = object({
    bucket_name  = string
    prefix       = string
    bucket_owner = string
    kms_key_arns = optional(set(string), [])
  })
  nullable = false

  validation {
    condition = try(
      length(trimspace(var.published_documents.bucket_name)) > 0 &&
      var.published_documents.bucket_name == trimspace(var.published_documents.bucket_name) &&
      can(regex("^[0-9]{12}$", var.published_documents.bucket_owner)) &&
      length(var.published_documents.prefix) > 1 &&
      !startswith(var.published_documents.prefix, "/") &&
      endswith(var.published_documents.prefix, "/") &&
      !strcontains(var.published_documents.prefix, "\\") &&
      alltrue([
        for segment in split(
          "/",
          trimsuffix(var.published_documents.prefix, "/")
        ) :
        segment != "" &&
        segment != "." &&
        segment != ".." &&
        segment == trimspace(segment)
      ]),
      false
    )

    error_message = "published_documents must specify a bucket, a 12-digit owner and an explicit prefix ending in '/'."
  }
}

variable "dms_resources" {
  description = "DMS resources the controller is allowed to operate."
  type = object({
    replication_task_arns     = set(string)
    replication_instance_arns = set(string)
    source_endpoint_arns      = set(string)
  })
  nullable = false

  validation {
    condition = try(
      length(var.dms_resources.replication_task_arns) > 0 &&
      length(var.dms_resources.replication_instance_arns) > 0 &&
      length(var.dms_resources.source_endpoint_arns) > 0 &&
      alltrue([
        for arn in setunion(
          var.dms_resources.replication_task_arns,
          var.dms_resources.replication_instance_arns,
          var.dms_resources.source_endpoint_arns
        ) :
        length(trimspace(arn)) > 0 && arn == trimspace(arn)
      ]),
      false
    )

    error_message = "dms_resources must include non-empty sets of task, instance and source endpoint ARNs."
  }
}

variable "source_credentials" {
  description = "Source preparation mode and optional credential synchronisation function."
  type = object({
    mode              = string
    sync_function_arn = optional(string)
  })
  nullable = false

  validation {
    condition = (
      var.source_credentials.mode == "connection_test"
      ? var.source_credentials.sync_function_arn == null
      : var.source_credentials.mode == "synchronise_then_test"
      ? try(
        length(trimspace(var.source_credentials.sync_function_arn)) > 0 &&
        var.source_credentials.sync_function_arn == trimspace(var.source_credentials.sync_function_arn),
        false
      )
      : false
    )

    error_message = "Use connection_test without a sync ARN, or synchronise_then_test with a non-empty sync ARN."
  }
}

variable "log_retention_days" {
  description = "CloudWatch log retention period in days."
  type        = number
  default     = 30
  nullable    = false
}

variable "tags" {
  description = "Tags applied to controller resources."
  type        = map(string)
  default     = {}
  nullable    = false
}
