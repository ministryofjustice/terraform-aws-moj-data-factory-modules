locals {
  create_source_secrets_access_role = (
    var.source_endpoint.secrets_manager_access_role_arn == null
  )

  create_s3_target_access_role = (
    var.s3_target_endpoint.service_access_role_arn == null
  )

  s3_target_object_arn = (
    var.s3_target_endpoint.bucket_folder != null
    && length(trim(var.s3_target_endpoint.bucket_folder, "/")) > 0
    ? "arn:aws:s3:::${var.s3_target_endpoint.bucket_name}/${trim(var.s3_target_endpoint.bucket_folder, "/")}/*"
    : "arn:aws:s3:::${var.s3_target_endpoint.bucket_name}/*"
  )
}

data "aws_iam_policy_document" "dms_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["dms.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "source_secrets_access" {
  count = local.create_source_secrets_access_role ? 1 : 0

  name = "${var.name}-dms-source-secrets"

  assume_role_policy = data.aws_iam_policy_document.dms_assume_role.json

  tags = merge(
    var.tags,
    {
      Name = "${var.name}-dms-source-secrets"
    }
  )
}

data "aws_iam_policy_document" "source_secrets_access" {
  count = local.create_source_secrets_access_role ? 1 : 0

  statement {
    sid = "ReadSourceSecret"

    effect = "Allow"

    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret"
    ]

    resources = [
      var.source_endpoint.secrets_manager_arn
    ]
  }

  dynamic "statement" {
    for_each = var.source_endpoint.secrets_manager_kms_key_arn != null ? [1] : []

    content {
      sid = "DecryptSourceSecret"

      effect = "Allow"

      actions = [
        "kms:Decrypt",
        "kms:DescribeKey"
      ]

      resources = [
        var.source_endpoint.secrets_manager_kms_key_arn
      ]
    }
  }
}

resource "aws_iam_role_policy" "source_secrets_access" {
  count = local.create_source_secrets_access_role ? 1 : 0

  name   = "${var.name}-dms-source-secrets"
  role   = aws_iam_role.source_secrets_access[0].id
  policy = data.aws_iam_policy_document.source_secrets_access[0].json
}

resource "aws_iam_role" "s3_target_access" {
  count = local.create_s3_target_access_role ? 1 : 0

  name = "${var.name}-dms-s3-target"

  assume_role_policy = data.aws_iam_policy_document.dms_assume_role.json

  tags = merge(
    var.tags,
    {
      Name = "${var.name}-dms-s3-target"
    }
  )
}

data "aws_iam_policy_document" "s3_target_access" {
  count = local.create_s3_target_access_role ? 1 : 0

  statement {
    sid = "AccessTargetBucket"

    effect = "Allow"

    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation"
    ]

    resources = [
      "arn:aws:s3:::${var.s3_target_endpoint.bucket_name}"
    ]
  }

  statement {
    sid = "AccessTargetObjects"

    effect = "Allow"

    actions = [
      "s3:DeleteObject",
      "s3:GetObject",
      "s3:PutObject",
      "s3:PutObjectTagging"
    ]

    resources = [
      local.s3_target_object_arn
    ]
  }

  dynamic "statement" {
    for_each = var.s3_target_endpoint.server_side_encryption_kms_key_arn != null ? [1] : []

    content {
      sid = "UseTargetEncryptionKey"

      effect = "Allow"

      actions = [
        "kms:Decrypt",
        "kms:DescribeKey",
        "kms:Encrypt",
        "kms:GenerateDataKey"
      ]

      resources = [
        var.s3_target_endpoint.server_side_encryption_kms_key_arn
      ]
    }
  }
}

resource "aws_iam_role_policy" "s3_target_access" {
  count = local.create_s3_target_access_role ? 1 : 0

  name   = "${var.name}-dms-s3-target"
  role   = aws_iam_role.s3_target_access[0].id
  policy = data.aws_iam_policy_document.s3_target_access[0].json
}

locals {
  source_secrets_manager_access_role_arn = (
    local.create_source_secrets_access_role
    ? aws_iam_role.source_secrets_access[0].arn
    : var.source_endpoint.secrets_manager_access_role_arn
  )

  s3_target_service_access_role_arn = (
    local.create_s3_target_access_role
    ? aws_iam_role.s3_target_access[0].arn
    : var.s3_target_endpoint.service_access_role_arn
  )
}