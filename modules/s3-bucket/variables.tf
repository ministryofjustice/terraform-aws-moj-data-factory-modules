variable "bucket_prefix" {
  description = "Prefix for the S3 bucket to create."
  type        = string
}

# Customer-managed KMS key used to encrypt objects in the bucket.
variable "kms_key_arn" {
  description = "ARN of the customer-managed KMS key used for S3 encryption."
  type        = string
}

variable "landing_bucket" {
    description = "Whether the bucket is a landing bucket for external data."
    type        = bool
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
#     Application = "corporate"
#     Owner       = "data-engineering"
#   }
variable "tags" {
  description = "Tags to apply to created resources."
  type        = map(string)
  default     = {}
}
