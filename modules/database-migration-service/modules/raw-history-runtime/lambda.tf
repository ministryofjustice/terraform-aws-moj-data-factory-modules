resource "aws_cloudwatch_log_group" "runtime" {
  name              = "/aws/lambda/${var.function_name}"
  retention_in_days = var.log_retention_days

  tags = var.tags
}

resource "aws_lambda_function" "runtime" {
  function_name = var.function_name
  role          = aws_iam_role.runtime.arn

  package_type = "Zip"
  runtime      = "python3.12"
  handler      = "dms_runtime.raw_history_bootstrap.lambda_handler"

  architectures = ["x86_64"]

  s3_bucket         = var.artifact.bucket_name
  s3_key            = var.artifact.object_key
  s3_object_version = var.artifact.version_id
  source_code_hash  = var.artifact.source_code_hash

  publish = true

  timeout     = 900
  memory_size = 512

  environment {
    variables = {
      RUNTIME_ENVIRONMENT           = var.service_context.environment
      RUNTIME_ACCOUNT_ID            = var.service_context.account_id
      RUNTIME_SOURCE                = var.service_context.source
      RUNTIME_DATABASE_NAME         = var.service_context.database_name
      INGESTION_REGISTRY_TABLE_NAME = var.registry.table_name

      RAW_HISTORY_CONFIGURATION = jsonencode({
        bucket_name = var.configuration_store.bucket_name
        object_key  = aws_s3_object.configuration.key
        version_id  = aws_s3_object.configuration.version_id
        sha256      = local.configuration_sha256
      })
    }
  }

  lifecycle {
    precondition {
      condition = alltrue([
        for arn in local.runtime_kms_key_arns :
        startswith(
          arn,
          "arn:${data.aws_partition.current.partition}:kms:${var.service_context.region}:"
        ) &&
        can(regex(
          "^arn:[^:]+:kms:[^:]+:[0-9]{12}:key/[A-Za-z0-9-]+$",
          arn
        ))
      ])

      error_message = "Runtime KMS permissions require explicit key ARNs in the runtime region, without aliases or wildcards."
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.runtime,
    aws_iam_role_policy.runtime,
  ]

  tags = var.tags
}
