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
  name_prefix        = "dms-runtime-"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
  tags               = var.tags
}

data "aws_iam_policy_document" "runtime" {
  statement {
    sid    = "WriteControllerLogs"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]

    resources = [
      "arn:${data.aws_partition.current.partition}:logs:${var.service_context.region}:${var.service_context.account_id}:log-group:/aws/lambda/${var.function_name}:*",
    ]
  }

  statement {
    sid    = "ReadAndUpdateRegistry"
    effect = "Allow"

    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:ConditionCheckItem",
    ]

    resources = [var.registry.table_arn]
  }

  statement {
    sid       = "ReadPublishedDocumentVersions"
    effect    = "Allow"
    actions   = ["s3:GetObjectVersion"]
    resources = ["arn:aws:s3:::${var.published_documents.bucket_name}/${var.published_documents.prefix}*"]
  }

  dynamic "statement" {
    for_each = length(var.published_documents.kms_key_arns) > 0 ? [1] : []

    content {
      sid       = "DecryptPublishedDocuments"
      effect    = "Allow"
      actions   = ["kms:Decrypt"]
      resources = sort(tolist(var.published_documents.kms_key_arns))
    }
  }

  statement {
    sid    = "DescribeDMSResources"
    effect = "Allow"

    actions = [
      "dms:DescribeConnections",
      "dms:DescribeReplicationTasks",
    ]

    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.service_context.region]
    }
  }

  statement {
    sid    = "InspectAndControlTasks"
    effect = "Allow"

    actions = [
      "dms:DescribeTableStatistics",
      "dms:StartReplicationTask",
      "dms:StopReplicationTask",
    ]

    resources = sort(tolist(var.dms_resources.replication_task_arns))
  }

  statement {
    sid     = "TestSourceConnection"
    effect  = "Allow"
    actions = ["dms:TestConnection"]

    resources = sort(tolist(setunion(
      var.dms_resources.replication_instance_arns,
      var.dms_resources.source_endpoint_arns
    )))
  }

  dynamic "statement" {
    for_each = var.source_credentials.mode == "synchronise_then_test" ? [1] : []

    content {
      sid       = "SynchroniseSourceCredentials"
      effect    = "Allow"
      actions   = ["lambda:InvokeFunction"]
      resources = [var.source_credentials.sync_function_arn]
    }
  }
}

resource "aws_iam_role_policy" "runtime" {
  name   = "dms-runtime-controller"
  role   = aws_iam_role.runtime.id
  policy = data.aws_iam_policy_document.runtime.json
}
