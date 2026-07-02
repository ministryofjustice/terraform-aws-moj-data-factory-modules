variable "bucket_name" {
  description = "Name of the S3 bucket to create."
  type        = string
}


# Customer-managed KMS key used to encrypt objects in the bucket.
variable "kms_key_arn" {
  description = "ARN of the customer-managed KMS key used for S3 encryption."
  type        = string
}

# IAM roles allowed to upload objects into this bucket used by the landing-bucket transfer process.
variable "writer_role_arns" {
  description = "IAM role ARNs allowed to write objects into the bucket."
  type        = list(string)
  default     = []
}

# IAM roles allowed to read objects after GuardDuty marks them as clean.
# The bucket policy uses the object tag:
#   GuardDutyMalwareScanStatus = NO_THREATS_FOUND
variable "clean_reader_role_arns" {
  description = "IAM role ARNs allowed to read objects only after GuardDuty marks them clean."
  type        = list(string)
  default     = []
}

# Optional list of S3 prefixes that GuardDuty should scan. If this is empty, GuardDuty scans the entire bucket.
variable "object_prefixes" {
  description = "Optional prefixes GuardDuty should scan. Empty list scans the whole bucket."
  type        = list(string)
  default     = []
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

variable "enable_quarantine" {
  description = "Whether to create the quarantine bucket, Lambda, and EventBridge rule."
  type        = bool
  default     = true
}