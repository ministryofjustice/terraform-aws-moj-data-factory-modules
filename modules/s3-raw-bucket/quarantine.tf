
# Creates a separate S3 bucket used to store objects that fail a GuardDuty malware scan.

# Objects are moved here when GuardDuty reports a bad or unusable scan result,
# such as:

# - THREATS_FOUND
# - FAILED
# - ACCESS_DENIED

module "quarantine_bucket" {

  count = var.enable_quarantine ? 1 : 0

  source  = "terraform-aws-modules/s3-bucket/aws"
  version = "~> 5.0"

  bucket        = "${var.bucket_name}-quarantine"
  force_destroy = var.force_destroy

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true

  control_object_ownership = true
  object_ownership         = "BucketOwnerEnforced"

  versioning = {
    enabled = true
  }

  server_side_encryption_configuration = {
    rule = {
      bucket_key_enabled = true

      apply_server_side_encryption_by_default = {
        sse_algorithm     = "aws:kms"
        kms_master_key_id = var.kms_key_arn
      }
    }
  }

  tags = merge(
    local.common_tags,
    {
      Purpose = "quarantine"
    }
  )
}

