locals {
  s3_arn_prefix = "arn:${data.aws_partition.current.partition}:s3:::"

  input_object_arns = distinct([
    for location in local.input_locations :
    "${local.s3_arn_prefix}${location.bucket_name}/${location.prefix}*"
    if !(
      location.bucket_name == var.configuration_store.bucket_name &&
      location.prefix == var.configuration_store.prefix
    )
  ])

  history_object_arns = distinct([
    for table in var.tables :
    "${local.s3_arn_prefix}${var.history_bucket.bucket_name}/${table.history_prefix}*"
  ])

  input_s3_kms_key_arns = distinct(concat(
    [var.configuration_store.kms_key_arn],
    tolist(var.published_documents.kms_key_arns),
    flatten([
      for table in var.tables : concat(
        flatten([
          for landing in table.landings :
          tolist(landing.kms_key_arns)
        ]),
        tolist(table.manifest_location.kms_key_arns),
        tolist(table.contract_location.kms_key_arns)
      )
    ])
  ))

  s3_decrypt_key_arns = distinct(concat(
    local.input_s3_kms_key_arns,
    [var.history_bucket.kms_key_arn]
  ))

  runtime_kms_key_arns = distinct(concat(
    local.s3_decrypt_key_arns,
    [var.operations_kms_key_arn],
    var.registry_kms_key_arn == null ? [] : [var.registry_kms_key_arn]
  ))
}

data "aws_iam_policy_document" "assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "runtime" {
  name_prefix = "${substr(var.function_name, 0, 29)}-history-"

  assume_role_policy = data.aws_iam_policy_document.assume_role.json

  tags = var.tags
}

data "aws_iam_policy_document" "runtime" {
  statement {
    sid    = "WriteLogs"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]

    resources = [
      "${aws_cloudwatch_log_group.runtime.arn}:*",
    ]
  }

  statement {
    sid       = "ReadRegistry"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem"]
    resources = [var.registry.table_arn]

    condition {
      test     = "ForAllValues:StringLike"
      variable = "dynamodb:LeadingKeys"
      values   = ["INGESTION#*"]
    }

    condition {
      test     = "Null"
      variable = "dynamodb:LeadingKeys"
      values   = ["false"]
    }
  }

  statement {
    sid    = "ManageOwnOperations"
    effect = "Allow"

    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
    ]

    resources = [
      aws_dynamodb_table.operations.arn,
    ]
  }

  statement {
    sid     = "ReadReceiverConfiguration"
    effect  = "Allow"
    actions = ["s3:GetObjectVersion"]
    resources = [
      "${local.s3_arn_prefix}${var.configuration_store.bucket_name}/${aws_s3_object.configuration.key}",
    ]

    condition {
      test     = "StringEquals"
      variable = "s3:VersionId"
      values   = [aws_s3_object.configuration.version_id]
    }
  }

  statement {
    sid       = "ReadPinnedInputs"
    effect    = "Allow"
    actions   = ["s3:GetObjectVersion"]
    resources = local.input_object_arns
  }

  statement {
    sid    = "ManageHistoryObjects"
    effect = "Allow"

    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
    ]

    resources = local.history_object_arns
  }

  statement {
    sid       = "DecryptS3Objects"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = local.s3_decrypt_key_arns

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.service_context.region}.amazonaws.com"]
    }
  }

  statement {
    sid    = "EncryptHistoryObjects"
    effect = "Allow"

    actions = [
      "kms:Encrypt",
      "kms:GenerateDataKey",
    ]

    resources = [
      var.history_bucket.kms_key_arn,
    ]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.service_context.region}.amazonaws.com"]
    }
  }

  statement {
    sid    = "UseOperationsTableKey"
    effect = "Allow"

    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey",
    ]

    resources = [
      var.operations_kms_key_arn,
    ]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["dynamodb.${var.service_context.region}.amazonaws.com"]
    }
  }

  dynamic "statement" {
    for_each = var.registry_kms_key_arn == null ? [] : [
      var.registry_kms_key_arn,
    ]

    content {
      sid       = "DecryptRegistry"
      effect    = "Allow"
      actions   = ["kms:Decrypt"]
      resources = [statement.value]

      condition {
        test     = "StringEquals"
        variable = "kms:ViaService"
        values   = ["dynamodb.${var.service_context.region}.amazonaws.com"]
      }
    }
  }
}

resource "aws_iam_role_policy" "runtime" {
  name   = "raw-history-runtime"
  role   = aws_iam_role.runtime.id
  policy = data.aws_iam_policy_document.runtime.json
}
