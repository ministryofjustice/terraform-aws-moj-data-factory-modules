variable "database_name" {
  type        = string
  description = "The name of the Glue catalog database to create."

  validation {
    condition     = length(var.database_name) < 50
    error_message = "The database name must be less than 50 characters."
  }
}

variable "kms_key_arn" {
  description = "The ARN of the KMS key to use for encrypting the S3 bucket."
  type        = string
}

variable "tags" {
  type        = map(string)
  description = "Tags to apply to created resources."
  default     = {}
}
