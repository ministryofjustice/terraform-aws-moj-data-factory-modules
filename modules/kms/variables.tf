variable "deletion_window_in_days" {
  description = "The waiting period, specified in number of days. After the waiting period ends, AWS KMS deletes the KMS key. If you specify a value, it must be between `7` and `30`, inclusive. If you do not specify a value, it defaults to `30`"
  type        = number
  default     = null
}

variable "description" {
  description = "The description of the key as viewed in AWS console"
  type        = string
  default     = null
}


variable "enable_key_rotation" {
  description = "Specifies whether key rotation is enabled. Defaults to `true`"
  type        = bool
  default     = true
}

variable "key_admin_role_arns" {
  description = "IAM role ARNs that can adminster KMS key."
  type        = list(string)
  default     = []
}

variable "key_user_role_arns" {
  description = "IAM role ARNs that can use the KMS key for crytpo operations"
  type        = list(string)
  default     = []
}

variable "alias_name" {
  description = "Alias for the KMS key, without the alias/ prefix."
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags for KMS resources."
  type        = map(string)
  default     = {}
}