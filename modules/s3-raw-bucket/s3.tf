
# This module uses the official terraform-aws-modules/s3-bucket module to create and configure an S3 bucket. 
# The bucket is configured to be scanned by GuardDuty Malware Protection before downstream services are 
# allowed to read the objects.

# Documentation:
# https://registry.terraform.io/modules/terraform-aws-modules/s3-bucket/aws/latest

# This bucket is intended to receive data from a landing bucket.
# Newly uploaded objects will be scanned by GuardDuty Malware Protection before
# downstream services are permitted to read them.

module "bucket" {
  source = "git::https://github.com/terraform-aws-modules/terraform-aws-s3-bucket.git?ref=0c0fb28347cc253088fe3966dca67420d39fbbe9"

  bucket        = local.bucket_name
  force_destroy = var.force_destroy

  attach_deny_incorrect_encryption_headers  = false
  attach_deny_incorrect_kms_key_sse         = true
  attach_deny_insecure_transport_policy     = true
  attach_deny_unencrypted_object_uploads    = true
  attach_deny_ssec_encrypted_object_uploads = false




  # Public access settings to block public access to the bucket and its objects.
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true

  # Enforce bucket ownership and disable ACLs to ensure that the bucket owner has full
  # control over the bucket and its objects.
  control_object_ownership = true
  object_ownership         = "BucketOwnerEnforced"

  # Enable versioning to keep track of object versions and allow recovery from accidental deletions or overwrites.
  versioning = {
    enabled = true
  }

  # Every object in the bucket will be encrypted using the customer-managed KMS key provided by the user.
  server_side_encryption_configuration = {
    rule = {
      bucket_key_enabled = true

      apply_server_side_encryption_by_default = {
        sse_algorithm     = "aws:kms"
        kms_master_key_id = var.kms_key_arn
      }
    }
  }

  # Optional lifecycle rules for the bucket, passed directly to the S3 bucket module.
  lifecycle_rule = var.lifecycle_rules

  tags = local.common_tags
}