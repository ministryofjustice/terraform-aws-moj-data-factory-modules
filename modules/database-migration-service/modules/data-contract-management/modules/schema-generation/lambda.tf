resource "aws_cloudwatch_log_group" "generation" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_in_days
  kms_key_id        = var.log_kms_key_arn

  tags = var.tags
}

resource "aws_lambda_function" "generation" {
  function_name = var.name
  description   = "Generates structurally validated Avro contracts from configured source metadata."

  role         = aws_iam_role.generation.arn
  package_type = "Image"
  image_uri    = var.image_uri

  architectures = [var.architecture]

  timeout     = var.timeout_seconds
  memory_size = var.memory_size_mb

  reserved_concurrent_executions = var.reserved_concurrent_executions
  kms_key_arn                    = var.lambda_environment_kms_key_arn

  environment {
    variables = local.generation_environment
  }

  vpc_config {
    subnet_ids         = sort(tolist(var.network.subnet_ids))
    security_group_ids = sort(tolist(var.network.security_group_ids))
  }

  depends_on = [
    aws_iam_role_policy.generation,
    aws_cloudwatch_log_group.generation,
  ]

  tags = var.tags
}
