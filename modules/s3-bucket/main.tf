# This module uses the official terraform-aws-modules/s3-bucket module to create and configure an S3 bucket. 

# Documentation:
# https://registry.terraform.io/modules/terraform-aws-modules/s3-bucket/aws/latest

module "bucket" {
  source = "git::https://github.com/terraform-aws-modules/terraform-aws-s3-bucket.git?ref=0c0fb28347cc253088fe3966dca67420d39fbbe9"

  bucket        = local.bucket_name
  force_destroy = var.force_destroy

  attach_deny_incorrect_encryption_headers  = false
  attach_deny_incorrect_kms_key_sse         = true
  allowed_kms_key_arn                       = var.kms_key_arn
  attach_deny_insecure_transport_policy     = true
  attach_deny_unencrypted_object_uploads    = true

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
      blocked_encryption_types = ["SSE-C"]

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

# Role that GuardDuty needs enable tagging and scanning, see: https://docs.aws.amazon.com/guardduty/latest/ug/malware-protection-s3-iam-policy-prerequisite.html
module "guarddduty_scan_role" {
  source = "git::https://github.com/terraform-aws-modules/terraform-aws-iam.git//modules/iam-role?ref=5b962b1163790398605f2b17447cf5b6cc512237"
  count = var.enable_malware_protection ? 1 : 0

  name            = "${module.bucket.bucket_name}-guardduty-malware"
  use_name_prefix = false

  description = "Allows GuardDuty to scan objects in ${module.bucket.bucket_name} for malware and tag them."

  create_inline_policy = true

  trust_policy_permissions = {
    AllowGuardDutyMalwareProtection = {
      actions = ["sts:AssumeRole"]

      principals = [{
        type = "Service"

        identifiers = [
          "malware-protection-plan.guardduty.amazonaws.com",
        ]
      }]
    }
  }


}

# Guard Duty S3 Malware Protection Plan, see: https://docs.aws.amazon.com/guardduty/latest/ug/malware-protection-s3.html
resource "aws_guardduty_malware_protection_plan" "malware_protection_plan" {
  count = var.enable_malware_protection ? 1 : 0

  role = module.guarddduty_scan_role[0].arn

  protected_resource {
    s3_bucket {
      bucket_name     = module.bucket.bucket_name
    }
  }

  actions {
    tagging {
      status = "ENABLED"
    }
  }

}