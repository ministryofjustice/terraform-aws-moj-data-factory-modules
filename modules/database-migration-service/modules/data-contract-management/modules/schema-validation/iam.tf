data "aws_partition" "current" {}

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

resource "aws_iam_role" "validation" {
  name                 = "${var.name}-execution"
  assume_role_policy   = data.aws_iam_policy_document.assume_role.json
  permissions_boundary = var.permissions_boundary_arn

  tags = var.tags
}

data "aws_iam_policy_document" "validation" {
  statement {
    sid       = "ReadRegisteredContracts"
    effect    = "Allow"
    actions   = ["dynamodb:Query"]
    resources = [var.registry_table_arn]
  }

  statement {
    sid       = "WriteValidationResults"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.validation_audit.arn]
  }

  statement {
    sid    = "ReadIncomingObjects"
    effect = "Allow"

    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
    ]

    resources = sort(tolist(local.incoming_object_arns))
  }

  statement {
    sid    = "ReadContractObjects"
    effect = "Allow"

    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
    ]

    resources = sort(tolist(var.contract_object_arns))
  }

  statement {
    sid    = "WriteFunctionLogs"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]

    resources = [
      "${aws_cloudwatch_log_group.validation.arn}:*",
    ]
  }

  statement {
    sid       = "SendFailedInvocations"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.failures.arn]
  }

  dynamic "statement" {
    for_each = length(local.s3_read_kms_key_arns) > 0 ? [1] : []

    content {
      sid       = "DecryptS3Objects"
      effect    = "Allow"
      actions   = ["kms:Decrypt"]
      resources = sort(tolist(local.s3_read_kms_key_arns))
    }
  }

  dynamic "statement" {
    for_each = length(var.registry_kms_key_arns) > 0 ? [1] : []

    content {
      sid    = "ReadEncryptedRegistry"
      effect = "Allow"

      actions = [
        "kms:Decrypt",
        "kms:DescribeKey",
      ]

      resources = sort(tolist(var.registry_kms_key_arns))
    }
  }

  statement {
    sid    = "UseValidationAuditEncryption"
    effect = "Allow"

    actions = [
      "kms:Decrypt",
      "kms:DescribeKey",
      "kms:Encrypt",
      "kms:GenerateDataKey",
      "kms:ReEncrypt*",
    ]

    resources = [var.validation_table_kms_key_arn]
  }

  dynamic "statement" {
    for_each = var.failure_queue_kms_key_arn == null ? [] : [1]

    content {
      sid    = "EncryptFailureMessages"
      effect = "Allow"

      actions = [
        "kms:Decrypt",
        "kms:GenerateDataKey",
      ]

      resources = [var.failure_queue_kms_key_arn]
    }
  }

  dynamic "statement" {
    for_each = var.lambda_environment_kms_key_arn == null ? [] : [1]

    content {
      sid       = "DecryptFunctionEnvironment"
      effect    = "Allow"
      actions   = ["kms:Decrypt"]
      resources = [var.lambda_environment_kms_key_arn]
    }
  }
}

resource "aws_iam_role_policy" "validation" {
  name   = "schema-validation"
  role   = aws_iam_role.validation.id
  policy = data.aws_iam_policy_document.validation.json
}
