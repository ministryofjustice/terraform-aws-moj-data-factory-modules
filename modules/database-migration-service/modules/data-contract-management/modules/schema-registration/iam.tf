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

resource "aws_iam_role" "registration" {
  name                 = "${var.name}-execution"
  assume_role_policy   = data.aws_iam_policy_document.assume_role.json
  permissions_boundary = var.permissions_boundary_arn

  tags = var.tags
}

data "aws_iam_policy_document" "registration" {
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
    sid    = "RegisterAndReadContracts"
    effect = "Allow"

    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
    ]

    resources = [aws_dynamodb_table.contracts.arn]
  }

  statement {
    sid    = "WriteFunctionLogs"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]

    resources = ["${aws_cloudwatch_log_group.registration.arn}:*"]
  }

  statement {
    sid       = "SendFailedInvocations"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.failures.arn]
  }

  dynamic "statement" {
    for_each = length(var.contract_kms_key_arns) > 0 ? [1] : []

    content {
      sid       = "DecryptContractObjects"
      effect    = "Allow"
      actions   = ["kms:Decrypt"]
      resources = sort(tolist(var.contract_kms_key_arns))
    }
  }

  statement {
    sid    = "UseRegistrationTableEncryption"
    effect = "Allow"

    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    ]

    resources = [var.table_kms_key_arn]
  }

  dynamic "statement" {
    for_each = var.failure_queue_kms_key_arn == null ? [] : [var.failure_queue_kms_key_arn]

    content {
      sid    = "EncryptFailureMessages"
      effect = "Allow"

      actions = [
        "kms:Decrypt",
        "kms:GenerateDataKey",
      ]

      resources = [statement.value]
    }
  }

  dynamic "statement" {
    for_each = var.lambda_environment_kms_key_arn == null ? [] : [var.lambda_environment_kms_key_arn]

    content {
      sid       = "DecryptFunctionEnvironment"
      effect    = "Allow"
      actions   = ["kms:Decrypt"]
      resources = [statement.value]
    }
  }
}

resource "aws_iam_role_policy" "registration" {
  name   = "schema-registration"
  role   = aws_iam_role.registration.id
  policy = data.aws_iam_policy_document.registration.json
}