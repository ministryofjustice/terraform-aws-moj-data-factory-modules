data "aws_partition" "current" {}

data "aws_region" "current" {}

data "aws_caller_identity" "current" {}

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

resource "aws_iam_role" "generation" {
  name                 = "${var.name}-execution"
  assume_role_policy   = data.aws_iam_policy_document.assume_role.json
  permissions_boundary = var.permissions_boundary_arn

  tags = var.tags
}

data "aws_iam_policy_document" "generation" {
  statement {
    sid       = "ReadDatabaseSecret"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.source_config.secret_arn]
  }

  statement {
    sid       = "ReadExistingContracts"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = sort(tolist(var.contract_object_arns))
  }

  statement {
    sid       = "PublishNewContracts"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = sort(tolist(var.contract_object_arns))

    condition {
      test     = "StringEquals"
      variable = "s3:if-none-match"
      values   = ["*"]
    }
  }

  statement {
    sid    = "WriteFunctionLogs"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]

    resources = ["${aws_cloudwatch_log_group.generation.arn}:*"]
  }

  statement {
    sid       = "SendFailedInvocations"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.failures.arn]
  }

  statement {
    sid    = "ManageLambdaNetworkInterfaces"
    effect = "Allow"

    actions = [
      "ec2:CreateNetworkInterface",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DescribeSubnets",
      "ec2:DeleteNetworkInterface",
      "ec2:AssignPrivateIpAddresses",
      "ec2:UnassignPrivateIpAddresses",
    ]

    resources = ["*"]
  }

  statement {
    sid    = "DenyNetworkManagementFromFunctionCode"
    effect = "Deny"

    actions = [
      "ec2:CreateNetworkInterface",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DescribeSubnets",
      "ec2:DeleteNetworkInterface",
      "ec2:DetachNetworkInterface",
      "ec2:AssignPrivateIpAddresses",
      "ec2:UnassignPrivateIpAddresses",
    ]

    resources = ["*"]

    condition {
      test     = "ArnEquals"
      variable = "lambda:SourceFunctionArn"
      values = [
        "arn:${data.aws_partition.current.partition}:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:${var.name}",
      ]
    }
  }

  dynamic "statement" {
    for_each = length(var.secret_kms_key_arns) > 0 ? [1] : []

    content {
      sid       = "DecryptDatabaseSecret"
      effect    = "Allow"
      actions   = ["kms:Decrypt"]
      resources = sort(tolist(var.secret_kms_key_arns))
    }
  }

  dynamic "statement" {
    for_each = length(var.contract_kms_key_arns) > 0 ? [1] : []

    content {
      sid    = "UseContractEncryption"
      effect = "Allow"

      actions = [
        "kms:Decrypt",
        "kms:GenerateDataKey",
      ]

      resources = sort(tolist(var.contract_kms_key_arns))
    }
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

resource "aws_iam_role_policy" "generation" {
  name   = "schema-generation"
  role   = aws_iam_role.generation.id
  policy = data.aws_iam_policy_document.generation.json
}
