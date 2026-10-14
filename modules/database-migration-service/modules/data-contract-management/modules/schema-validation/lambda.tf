resource "aws_cloudwatch_log_group" "validation" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_in_days
  kms_key_id        = var.log_kms_key_arn

  tags = var.tags
}

resource "aws_lambda_function" "validation" {
  function_name = var.name
  description   = "Validates incoming Parquet schemas against registered Avro contracts."

  role         = aws_iam_role.validation.arn
  package_type = "Image"
  image_uri    = var.image_uri

  architectures = [var.architecture]

  timeout     = var.timeout_seconds
  memory_size = var.memory_size_mb

  reserved_concurrent_executions = var.reserved_concurrent_executions
  kms_key_arn                    = var.lambda_environment_kms_key_arn

  environment {
    variables = local.validation_environment
  }

  depends_on = [
    aws_iam_role_policy.validation,
    aws_cloudwatch_log_group.validation,
    aws_dynamodb_table.validation_audit,
  ]

  tags = var.tags

  lifecycle {
    precondition {
      condition     = local.validation_environment_size_bytes <= 4096
      error_message = "Validation environment variables exceed Lambda's 4096-byte limit. Reduce the mappings per deployment or implement external configuration loading."
    }
  }
}
