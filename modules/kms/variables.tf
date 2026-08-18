variable "description" {
  description = "The description of the key as viewed in AWS console"
  type        = string
}

variable "enable_key_rotation" {
  description = "Automatically rotates the key material annually. Recommended to keep true."
  type        = bool
  default     = true
}

variable "deletion_window_in_days" {
  description = "Days to wait before permanently deleting the key after a terraform destroy. Must be between 7 and 30."
  type        = number
  default     = 7

  validation {
    condition     = var.deletion_window_in_days >= 7 && var.deletion_window_in_days <= 30
    error_message = "deletion_window_in_days must be between 7 and 30."
  }
}

variable "key_service_users" {
  description = "IAM roles for AWS services (e.g. EC2, Lambda) that need to create/manage KMS grants on behalf of AWS-integrated services. Grants kms:CreateGrant, kms:ListGrants, kms:RevokeGrant. Not required for cross-account IAM role access — use the IAM role's inline policy instead."
  type        = list(string)
  default     = []
}

variable "key_administrators" {
  description = "IAM roles that can manage the key lifecycle: rotate, disable, schedule deletion, update policy. These roles cannot encrypt or decrypt data — administration only."
  type        = list(string)
  default     = []
}

variable "policy" {
  description = "A valid policy JSON document to override the auto-generated key policy. If null, the policy is built from enable_default_policy, key_administrators, and key_service_users"
  type        = string
  default     = null
}



variable "aliases" {
  description = "A list of aliases to create for the KMS key"
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags for KMS resources"
  type        = map(string)
  default     = {}
}
