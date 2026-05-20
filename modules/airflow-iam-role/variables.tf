variable "environment" {
  description = "Application name used in the MWAA service account identifier (use `local.application_name` as defined in `platform.locals.tf`)"
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

variable "application_name" {
  description = "Application name used in the MWAA service account identifier. Use `local.application_name`"
  type        = string
}

variable "role_name_suffix" {
  description = "Suffix used when constructing the IAM role name"
  type        = string
}

variable "role_description" {
  description = "Description of the IAM role"
  type        = string
}

variable "oidc_arn" {
  description = "ARN of the OIDC identity provider"
  type        = string
}

variable "secret_code" {
  description = "OIDC issuer ID segment used in condition keys"
  type        = string
}

variable "iam_policy_documents" {
  description = "List of IAM policy JSON documents to create and attach to the role"
  type        = list(string)
}

variable "max_session_duration" {
  description = "Maximum session duration in seconds"
  type        = number
  default     = 3600
}
