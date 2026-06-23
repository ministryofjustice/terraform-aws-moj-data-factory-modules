variable "environment" {
  description = "Deployment environment name (use `local.environment` as defined in `platform_locals.tf`)"
  type        = string

  validation {
    condition = contains([
      "production",
      "preproduction",
      "test",
      "development"
    ], var.environment)
    error_message = "environment must be one of: production, preproduction, test, development."
  }
}

variable "name_prefix" {
  description = "Prefix used to construct the bucket name. Final name: `<name_prefix>-quarantine-<env>`."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,40}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be 3-42 chars, lowercase alphanumeric or hyphen, and start/end with an alphanumeric character."
  }
}


# Trusted principals (isolation)

variable "writer_role_arns" {
  description = "IAM role ARNs allowed to write to the bucket (typically the scan/quarantine Lambda role). All other principals are denied. The bucket-owning account root is always trusted to avoid lockout."
  type        = list(string)
  default     = []
}

variable "reader_role_arns" {
  description = "IAM role ARNs allowed to read from the bucket (typically the security/incident-response role). All other principals are denied."
  type        = list(string)
  default     = []
}

variable "additional_trusted_principal_arns" {
  description = "Additional principal ARNs (or `aws:PrincipalArn` wildcard patterns) exempt from the deny-by-default bucket policy, e.g. a Terraform deployment role."
  type        = list(string)
  default     = []
}


# Object Lock & lifecycle


variable "object_lock_retention_days" {
  description = "Number of days quarantined objects are protected by S3 Object Lock (GOVERNANCE mode)."
  type        = number
  default     = 90

  validation {
    condition     = var.object_lock_retention_days >= 1
    error_message = "object_lock_retention_days must be at least 1."
  }
}

variable "expiry_days" {
  description = "Number of days after which quarantined objects are expired by the lifecycle rule."
  type        = number
  default     = 90

  validation {
    condition     = var.expiry_days >= 1
    error_message = "expiry_days must be at least 1."
  }
}

variable "abort_incomplete_multipart_upload_days" {
  description = "Number of days after which incomplete multipart uploads are aborted."
  type        = number
  default     = 7
}


# Access logging


variable "enable_access_logging" {
  description = "Whether to enable S3 server access logging to a separate target bucket."
  type        = bool
  default     = false
}

variable "access_log_bucket" {
  description = "Target bucket ID for S3 server access logs. Required when `enable_access_logging` is true."
  type        = string
  default     = null
}

variable "access_log_prefix" {
  description = "Key prefix for S3 server access logs in the target bucket."
  type        = string
  default     = "quarantine-access-logs/"
}
