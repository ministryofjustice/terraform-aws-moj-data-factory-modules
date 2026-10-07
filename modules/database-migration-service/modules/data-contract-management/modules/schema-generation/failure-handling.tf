resource "aws_sqs_queue" "failures" {
  name = "${var.name}-failures"

  message_retention_seconds = 1209600

  kms_master_key_id = var.failure_queue_kms_key_arn

  sqs_managed_sse_enabled = (
    var.failure_queue_kms_key_arn == null ? true : null
  )

  tags = var.tags
}

data "aws_iam_policy_document" "failure_queue" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    actions   = ["sqs:*"]
    resources = [aws_sqs_queue.failures.arn]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "failures" {
  queue_url = aws_sqs_queue.failures.url
  policy    = data.aws_iam_policy_document.failure_queue.json
}

resource "aws_lambda_function_event_invoke_config" "generation" {
  function_name = aws_lambda_function.generation.function_name

  maximum_retry_attempts       = var.maximum_retry_attempts
  maximum_event_age_in_seconds = var.maximum_event_age_in_seconds

  destination_config {
    on_failure {
      destination = aws_sqs_queue.failures.arn
    }
  }

  depends_on = [
    aws_iam_role_policy.generation,
    aws_sqs_queue_policy.failures,
  ]
}
