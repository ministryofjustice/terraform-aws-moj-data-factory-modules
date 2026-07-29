locals {
  env = {
    "production"    = "prod"
    "preproduction" = "preprod"
    "test"          = "test"
    "development"   = "dev"
  }

  bucket_name = "${var.name_prefix}-quarantine-${local.env[var.environment]}"

  # KMS key used for the bucket and the SNS topic.
  kms_key_arn = aws_kms_key.this.arn

  # Principals exempt from the deny-by-default bucket policy. The account root is
  # always trusted so account admins / Terraform are not locked out.
  trusted_principal_arns = concat(
    var.writer_role_arns,
    var.reader_role_arns,
    var.additional_trusted_principal_arns,
    ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"],
  )
}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}


# Input validation

resource "terraform_data" "validations" {
  lifecycle {
    precondition {
      condition     = !var.enable_access_logging || var.access_log_bucket != null
      error_message = "access_log_bucket must be set when enable_access_logging is true."
    }
  }
}


# Dedicated KMS key

data "aws_iam_policy_document" "kms" {
  statement {
    sid       = "EnableRootAccountAdmin"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  dynamic "statement" {
    for_each = length(concat(var.writer_role_arns, var.reader_role_arns)) > 0 ? [1] : []
    content {
      sid    = "AllowQuarantineRolesUseOfKey"
      effect = "Allow"
      actions = [
        "kms:Decrypt",
        "kms:DescribeKey",
        "kms:GenerateDataKey*",
      ]
      resources = ["*"]
      principals {
        type        = "AWS"
        identifiers = concat(var.writer_role_arns, var.reader_role_arns)
      }
    }
  }

  # Required so S3 can encrypt notifications published to an encrypted SNS topic.
  statement {
    sid    = "AllowS3ServiceForSnsEncryption"
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey*",
    ]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_kms_key" "this" {
  description             = "Encryption key for the ${local.bucket_name} quarantine bucket"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.kms.json
}

resource "aws_kms_alias" "this" {
  name          = "alias/${local.bucket_name}"
  target_key_id = aws_kms_key.this.key_id
}


# Deny-by-default bucket policy


data "aws_iam_policy_document" "bucket" {
  dynamic "statement" {
    for_each = length(var.writer_role_arns) > 0 ? [1] : []
    content {
      sid    = "AllowQuarantineWritersBucketAccess"
      effect = "Allow"
      actions = [
        "s3:GetBucketLocation",
        "s3:ListBucketMultipartUploads",
      ]
      resources = ["_S3_BUCKET_ARN_"]

      principals {
        type        = "AWS"
        identifiers = var.writer_role_arns
      }
    }
  }

  dynamic "statement" {
    for_each = length(var.writer_role_arns) > 0 ? [1] : []
    content {
      sid    = "AllowQuarantineWritersObjectAccess"
      effect = "Allow"
      actions = [
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts",
        "s3:PutObject",
      ]
      resources = ["_S3_BUCKET_ARN_/*"]

      principals {
        type        = "AWS"
        identifiers = var.writer_role_arns
      }
    }
  }

  dynamic "statement" {
    for_each = length(var.reader_role_arns) > 0 ? [1] : []
    content {
      sid    = "AllowQuarantineReadersBucketAccess"
      effect = "Allow"
      actions = [
        "s3:GetBucketLocation",
        "s3:ListBucket",
      ]
      resources = ["_S3_BUCKET_ARN_"]

      principals {
        type        = "AWS"
        identifiers = var.reader_role_arns
      }
    }
  }

  dynamic "statement" {
    for_each = length(var.reader_role_arns) > 0 ? [1] : []
    content {
      sid    = "AllowQuarantineReadersObjectAccess"
      effect = "Allow"
      actions = [
        "s3:GetObject",
        "s3:GetObjectLegalHold",
        "s3:GetObjectRetention",
        "s3:GetObjectVersion",
      ]
      resources = ["_S3_BUCKET_ARN_/*"]

      principals {
        type        = "AWS"
        identifiers = var.reader_role_arns
      }
    }
  }

  statement {
    sid       = "DenyUntrustedPrincipals"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = ["_S3_BUCKET_ARN_", "_S3_BUCKET_ARN_/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringNotLike"
      variable = "aws:PrincipalArn"
      values   = local.trusted_principal_arns
    }
  }
}


# Quarantine bucket
module "quarantine_bucket" {
  source  = "terraform-aws-modules/s3-bucket/aws"
  version = "~> 5.14"

  bucket        = local.bucket_name
  force_destroy = false

  # Block all public access.
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true

  # Disable ACLs; the bucket owner owns every object.
  control_object_ownership = true
  object_ownership         = "BucketOwnerEnforced"

  # Versioning + Object Lock
  versioning = {
    enabled = true
  }
  object_lock_enabled = true
  object_lock_configuration = {
    rule = {
      default_retention = {
        mode = "GOVERNANCE"
        days = var.object_lock_retention_days
      }
    }
  }

  # Server-side encryption with the dedicated/customer-managed KMS key.
  server_side_encryption_configuration = {
    rule = {
      apply_server_side_encryption_by_default = {
        kms_master_key_id = local.kms_key_arn
        sse_algorithm     = "aws:kms"
      }
      bucket_key_enabled = true
    }
  }

  # Hardening policies (merged with the deny-by-default policy above).
  attach_policy                         = true
  policy                                = data.aws_iam_policy_document.bucket.json
  attach_deny_insecure_transport_policy = true
  attach_require_latest_tls_policy      = true

  # Expire quarantined objects and clean up incomplete uploads.
  lifecycle_rule = [
    {
      id      = "expire-quarantined-objects"
      enabled = true

      abort_incomplete_multipart_upload_days = var.abort_incomplete_multipart_upload_days

      expiration = {
        days = var.expiry_days
      }

      noncurrent_version_expiration = {
        days = var.expiry_days
      }
    }
  ]

  # Optional server access logging.
  logging = var.enable_access_logging ? {
    target_bucket = var.access_log_bucket
    target_prefix = var.access_log_prefix
  } : {}
}


# Notifications: S3 -> SNS
resource "aws_sns_topic" "this" {
  name              = "${local.bucket_name}-alerts"
  kms_master_key_id = local.kms_key_arn
}

data "aws_iam_policy_document" "sns" {
  statement {
    sid       = "AllowQuarantineBucketPublish"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.this.arn]

    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = [module.quarantine_bucket.s3_bucket_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "this" {
  arn    = aws_sns_topic.this.arn
  policy = data.aws_iam_policy_document.sns.json
}

resource "aws_s3_bucket_notification" "this" {
  bucket = module.quarantine_bucket.s3_bucket_id

  topic {
    topic_arn = aws_sns_topic.this.arn
    events    = ["s3:ObjectCreated:*"]
  }

  depends_on = [aws_sns_topic_policy.this]
}
