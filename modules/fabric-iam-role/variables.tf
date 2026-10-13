variable "additional_tags" {
  type        = map(string)
  description = "Additional tags to apply to resources created by this module."
  default     = {}
}

variable "oidc_provider_condition_key_prefix" {
  type        = string
  description = "Condition key prefix from the fabric-oidc-provider module output (e.g. 'sts.windows.net/{tenant_id}/:')."

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
  description = "Enterprise Application (service principal) Object ID of the Microsoft Entra identity allowed to assume the IAM role."
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
    condition     = can(regex("^[\\w+=,.@-]{1,64}$", var.role_name)) && trimspace(var.role_name) == var.role_name
    error_message = "role_name must be 1-64 characters, use only [A-Za-z0-9_+=,.@-], and have no leading or trailing whitespace."
  }
}

variable "role_policy_name" {
  type        = string
  description = "Inline IAM policy name attached to the IAM role."

  validation {
    condition     = can(regex("^[\\w+=,.@-]{1,128}$", var.role_policy_name)) && trimspace(var.role_policy_name) == var.role_policy_name
    error_message = "role_policy_name must be 1-128 characters, use only [A-Za-z0-9_+=,.@-], and have no leading or trailing whitespace."
  }
}
