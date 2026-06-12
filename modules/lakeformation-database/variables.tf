variable "database_name" {
  type        = string
  description = "The name of the Glue catalog database to create."
}

variable "kms_key_arn" {
  description = "The ARN of the KMS key to use for encrypting the S3 bucket."
  type        = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
