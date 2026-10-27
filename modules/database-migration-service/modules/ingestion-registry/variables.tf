variable "name" {
  description = "Name of the shared Ingestion Registry table."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]{3,255}$", var.name))
    error_message = "name must be 3–255 characters using letters, numbers, underscores, hyphens or full stops."
  }
}

variable "kms_key_arn" {
  description = "Customer-managed KMS key ARN used to encrypt the registry table."
  type        = string
  nullable    = false

  validation {
    condition = can(regex(
      "^arn:(aws|aws-us-gov|aws-cn):kms:[a-z0-9-]+:[0-9]{12}:key/[A-Za-z0-9-]+$",
      var.kms_key_arn
    ))

    error_message = "kms_key_arn must be a KMS key ARN."
  }
}

variable "deletion_protection_enabled" {
  description = "Protect the registry from accidental deletion."
  type        = bool
  default     = true
  nullable    = false
}

variable "tags" {
  description = "Tags applied to the registry table."
  type        = map(string)
  default     = {}
}
