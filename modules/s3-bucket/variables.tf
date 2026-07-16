variable "bucket_prefix" {
  description = "Prefix for the S3 bucket to create."
  type        = string
}

variable "environment" {
  description = "Environment for the S3 bucket to create."
  type        = string

  validation {
    condition     = contains(["dev", "test", "prod"], var.environment)
    error_message = "Environment must be one of 'dev', 'test', or 'prod'."
  }
}

# Customer-managed KMS key used to encrypt objects in the bucket.
variable "kms_key_arn" {
  description = "ARN of the customer-managed KMS key used for S3 encryption."
  type        = string
}

# Control whether Terraform can delete a non-empty bucket. Usually false outside dev.
variable "force_destroy" {
  description = "Whether Terraform can delete a non-empty bucket. Usually false outside dev."
  type        = bool
  default     = false
}

# Optional lifecycle rules for the bucket, passed directly to the S3 bucket module.
# See https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket#lifecycle_rule for details.
variable "lifecycle_rules" {
  description = "Optional lifecycle rules for the bucket."
  type        = any
  default     = []
}

# Optional tags to apply to created resources. These are merged with the standard tags applied by the module.
# Example:
#   {
#     Environment = "dev"
#     Application = "raw-data"
#     Owner       = "corp-data-eng"
#   }
variable "tags" {
  description = "Tags to apply to created resources."
  type        = map(string)
  default     = {}
}
