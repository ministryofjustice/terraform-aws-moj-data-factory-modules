resource "aws_cloudwatch_log_group" "registration" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_in_days
  kms_key_id        = var.log_kms_key_arn

  tags = var.tags
}

resource "aws_lambda_function" "registration" {
  function_name = var.name
  description   = "Registers validated contract versions and their metadata."

  role         = aws_iam_role.registration.arn
  package_type = "Image"
  image_uri    = var.image_uri

  architectures = [var.architecture]

  timeout     = var.timeout_seconds
  memory_size = var.memory_size_mb

  reserved_concurrent_executions = var.reserved_concurrent_executions
  kms_key_arn                    = var.lambda_environment_kms_key_arn

  environment {
    variables = {
      AUDIT_TABLE_NAME = aws_dynamodb_table.contracts.name
    }
  }

  depends_on = [
    aws_iam_role_policy.registration,
    aws_cloudwatch_log_group.registration,
  ]

  tags = var.tags
}