locals {
  processing_log_groups = {
    receiver = "/aws/lambda/${var.function_name}"
    worker   = "/aws/lambda/${var.function_name}-worker"
  }

  processing_log_roles = {
    receiver = aws_iam_role.receiver.name
    worker   = aws_iam_role.worker.name
  }
}

resource "aws_cloudwatch_log_group" "processing" {
  for_each = local.processing_log_groups

  name              = each.value
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

data "aws_iam_policy_document" "processing_logs" {
  for_each = local.processing_log_groups

  statement {
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "${aws_cloudwatch_log_group.processing[each.key].arn}:log-stream:*",
    ]
  }
}

resource "aws_iam_role_policy" "processing_logs" {
  for_each = local.processing_log_groups

  name   = "processing-logs"
  role   = local.processing_log_roles[each.key]
  policy = data.aws_iam_policy_document.processing_logs[each.key].json
}
