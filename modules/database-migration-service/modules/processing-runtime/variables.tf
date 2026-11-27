variable "function_name" {
  description = "Base name for the Processing receiver and worker."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{1,57}$", var.function_name))
    error_message = "function_name must contain 1–57 letters, numbers, underscores or hyphens, leaving room for the worker suffix."
  }
}

variable "operations_kms_key_arn" {
  description = "KMS key ARN for the Processing operations table."
  type        = string
  nullable    = false
}

variable "deletion_protection_enabled" {
  description = "Protect the Processing operations table against accidental deletion."
  type        = bool
  default     = true
  nullable    = false
}

variable "tags" {
  description = "Tags applied to resources created by the Processing runtime."
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
}

variable "input_bucket_owners" {
  description = "AWS account IDs that own the pinned Processing input buckets."
  type = object({
    raw      = string
    manifest = string
    contract = string
  })
  nullable = false

  validation {
    condition = alltrue([
      for owner in values(var.input_bucket_owners) :
      can(regex("^[0-9]{12}$", owner))
    ])
    error_message = "Each input bucket owner must be a 12-digit AWS account ID."
  }
}

variable "output" {
  description = "Versioned S3 destination for Processing data and result evidence."
  type = object({
    bucket_name  = string
    bucket_owner = string
    prefix       = string
  })
  nullable = false

  validation {
    condition = (
      can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.output.bucket_name)) &&
      can(regex("^[0-9]{12}$", var.output.bucket_owner)) &&
      endswith(var.output.prefix, "/") &&
      var.output.prefix == trimspace(var.output.prefix) &&
      !strcontains(var.output.prefix, "*") &&
      !strcontains(var.output.prefix, "?") &&
      !strcontains(var.output.prefix, "\\") &&
      alltrue([
        for part in split("/", trimsuffix(var.output.prefix, "/")) :
        !contains(["", ".", ".."], part)
      ])
    )
    error_message = "Provide an output bucket, a 12-digit owner and a relative prefix ending in '/', without wildcards."
  }
}

variable "max_batch_bytes" {
  description = "Maximum total size in bytes of the pinned Raw files in one batch."
  type        = number
  nullable    = false

  validation {
    condition     = var.max_batch_bytes > 0 && floor(var.max_batch_bytes) == var.max_batch_bytes
    error_message = "max_batch_bytes must be a positive integer."
  }
}

variable "log_retention_days" {
  description = "Retention in days for Processing Lambda logs."
  type        = number
  default     = 30
  nullable    = false
}

variable "input_locations" {
  description = "S3 bucket and prefix for each kind of pinned Processing input."
  type = map(object({
    bucket_name = string
    prefix      = string
  }))
  nullable = false

  validation {
    condition = (
      toset(keys(var.input_locations)) == toset(["raw", "manifest", "contract"]) &&
      alltrue([
        for location in values(var.input_locations) :
        can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", location.bucket_name)) &&
        endswith(location.prefix, "/") &&
        location.prefix == trimspace(location.prefix) &&
        !strcontains(location.prefix, "*") &&
        !strcontains(location.prefix, "?") &&
        !strcontains(location.prefix, "\\") &&
        alltrue([
          for part in split("/", trimsuffix(location.prefix, "/")) :
          !contains(["", ".", ".."], part)
        ])
      ])
    )
    error_message = "Provide raw, manifest and contract locations with bucket names and relative prefixes ending in '/', without wildcards."
  }
}

variable "input_kms_key_arns" {
  description = "KMS keys used to encrypt the pinned S3 inputs."
  type        = set(string)
  default     = []
  nullable    = false

  validation {
    condition = alltrue([
      for arn in var.input_kms_key_arns :
      can(regex("^arn:[a-z0-9-]+:kms:[a-z0-9-]+:[0-9]{12}:key/[A-Za-z0-9-]+$", arn))
    ])
    error_message = "Input KMS keys must be specified as explicit key ARNs."
  }
}

variable "output_kms_key_arn" {
  description = "KMS key used by the Processing output bucket, when it uses SSE-KMS."
  type        = string
  default     = null

  validation {
    condition = var.output_kms_key_arn == null || can(regex(
      "^arn:[a-z0-9-]+:kms:[a-z0-9-]+:[0-9]{12}:key/[A-Za-z0-9-]+$",
      var.output_kms_key_arn,
    ))
    error_message = "The output KMS key must be specified as an explicit key ARN."
  }
}
