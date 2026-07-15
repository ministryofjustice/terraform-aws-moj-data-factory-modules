variable "description" {
  description = "The description of the key as viewed in AWS console"
  type        = string
}

variable "enable_key_rotation" {
  description = "Specifies whether key rotation is enabled. Defaults to true"
  type        = bool
  default     = true
}

variable "deletion_window_in_days" {
  description = "The waiting period, specified in number of days. After the waiting period ends, AWS KMS deletes the KMS key. Must be between 7 and 30, inclusive"
  type        = number
  default     = 7

  validation {
    condition     = var.deletion_window_in_days >= 7 && var.deletion_window_in_days <= 30
    error_message = "deletion_window_in_days must be between 7 and 30."
  }
}

variable "key_service_users" {
  description = "A list of IAM ARNs that can use the key for encryption/decryption (e.g., Avature, Lambda, EC2 roles)"
  type        = list(string)
  default     = []
}

variable "key_administrators" {
  description = "A list of IAM ARNs that can administer the key (manage policy, rotation, etc.)"
  type        = list(string)
  default     = []
}

variable "is_enabled" {
  description = "Specifies whether the key is enabled. Defaults to true"
  type        = bool
  default     = true
}

variable "multi_region" {
  description = "Indicates whether the KMS key is a multi-Region key. Defaults to false"
  type        = bool
  default     = false
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
