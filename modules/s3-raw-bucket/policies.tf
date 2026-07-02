# Bucket Policy 

# This file defines the S3 bucket policy attached to the bucket. 
# The policy is responsible for: 
# - Denying insecure (HTTP) requests
# - Requiring SSE-KMS encryption for all uploads
# - Ensuring the correct KMS key is used
# - Allowing approved writer roles to upload objects
# - Allowing downstream reader roles to read only objects that have passed
#   a GuardDuty malware scan

# The resulting policy document is attached to the bucket by the S3 module in s3.tf.

data "aws_iam_policy_document" "bucket_policy" {

  # Prevents any request made over HTTP instead of HTTPS. 
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = [
      "s3:*"
    ]

    resources = [
      local.bucket_arn,
      local.bucket_objects_arn
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

# Reject uploads unless the caller explicitly requests SSE-KMS encryption.
  statement {
    sid    = "DenyIncorrectEncryptionHeader"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = [
      "s3:PutObject"
    ]

    resources = [
      local.bucket_objects_arn
    ]

    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["aws:kms"]
    }
  }

# Require uploads to use the correct KMS key
  statement {
    sid    = "DenyIncorrectKmsKey"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = [
      "s3:PutObject"
    ]

    resources = [
      local.bucket_objects_arn
    ]

    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = [var.kms_key_arn]
    }
  }

# These IAM roles are permitted to upload objects into the bucket.
  dynamic "statement" {
    for_each = length(var.writer_role_arns) > 0 ? [1] : []

    content {
      sid    = "AllowApprovedWriters"
      effect = "Allow"

      principals {
        type        = "AWS"
        identifiers = var.writer_role_arns
      }

      actions = [
        "s3:PutObject",
        "s3:PutObjectTagging",
        "s3:AbortMultipartUpload"
      ]

      resources = [
        local.bucket_objects_arn
      ]
    }
  }

# Reader roles may only retrieve an object if GuardDuty has scanned it and applied the object tag: 
# GuardDutyMalwareScanStatus = NO_THREATS_FOUND
  dynamic "statement" {
    for_each = length(var.clean_reader_role_arns) > 0 ? [1] : []

    content {
      sid    = "AllowReadOnlyCleanObjects"
      effect = "Allow"

      principals {
        type        = "AWS"
        identifiers = var.clean_reader_role_arns
      }

      actions = [
        "s3:GetObject",
        "s3:GetObjectVersion"
      ]

      resources = [
        local.bucket_objects_arn
      ]

      condition {
        test     = "StringEquals"
        variable = "s3:ExistingObjectTag/GuardDutyMalwareScanStatus"
        values   = ["NO_THREATS_FOUND"]
      }
    }
  }

# This statement grants ListBucket to the approved reader roles.
  dynamic "statement" {
    for_each = length(var.clean_reader_role_arns) > 0 ? [1] : []

    content {
      sid    = "AllowReadersToListBucket"
      effect = "Allow"

      principals {
        type        = "AWS"
        identifiers = var.clean_reader_role_arns
      }

      actions = [
        "s3:ListBucket"
      ]

      resources = [
        local.bucket_arn
      ]
    }
  }
}