variable "oidc_provider_condition_key_prefix" {
  type        = string
  description = "Condition key prefix from the oidc-provider module output (e.g. 'sts.windows.net/{tenant_id}/:')."

  validation {
    condition     = length(trimspace(var.oidc_provider_condition_key_prefix)) > 1 && endswith(trimspace(var.oidc_provider_condition_key_prefix), ":") && !strcontains(trimspace(var.oidc_provider_condition_key_prefix), " ")
    error_message = "oidc_provider_condition_key_prefix must be non-empty, must end with ':', and must not contain spaces."
  }
}

variable "audience" {
  type        = string
  description = "Expected OIDC audience (`aud`) claim. Defaults to the Power BI Amazon S3 connector audience."
  default     = "https://analysis.windows.net/powerbi/connector/AmazonS3"

  validation {
    condition     = length(trimspace(var.audience)) > 0
    error_message = "audience must be a non-empty string."
  }
}

variable "object_id" {
  type        = string
  description = "Microsoft Entra object ID allowed to assume the IAM role."

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", trimspace(var.object_id)))
    error_message = "object_id must be a non-empty GUID."
  }
}

variable "oidc_provider_arn" {
  type        = string
  description = "ARN of the IAM OIDC provider trusted by this role."

  validation {
    condition     = can(regex("^arn:[^:]+:iam::[0-9]{12}:oidc-provider/.+$", trimspace(var.oidc_provider_arn)))
    error_message = "oidc_provider_arn must be a non-empty IAM OIDC provider ARN."
  }
}

variable "bucket_arn" {
  type        = string
  description = "ARN of the S3 bucket this role can read."

  validation {
    condition     = can(regex("^arn:[^:]+:s3:::[^/]+$", trimspace(var.bucket_arn)))
    error_message = "bucket_arn must be a non-empty S3 bucket ARN."
  }
}

variable "role_name" {
  type        = string
  description = "IAM role name for Fabric web identity access."

  validation {
    condition     = length(trimspace(var.role_name)) > 0
    error_message = "role_name must be a non-empty string."
  }
}

variable "role_policy_name" {
  type        = string
  description = "Inline IAM policy name attached to the IAM role."

  validation {
    condition     = length(trimspace(var.role_policy_name)) > 0
    error_message = "role_policy_name must be a non-empty string."
  }
}
