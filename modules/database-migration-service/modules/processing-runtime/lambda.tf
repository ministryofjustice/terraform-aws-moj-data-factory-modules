resource "aws_lambda_function" "receiver" {
  function_name = var.function_name
  role          = aws_iam_role.receiver.arn

  package_type  = "Zip"
  runtime       = "python3.12"
  handler       = "dms_runtime.processing_lambda_function.receiver_handler"
  architectures = ["x86_64"]

  s3_bucket         = var.artifact.bucket_name
  s3_key            = var.artifact.object_key
  s3_object_version = var.artifact.version_id
  source_code_hash  = var.artifact.source_code_hash

  publish = true
  timeout = 30

  environment {
    variables = {
      PROCESSING_OPERATIONS_TABLE_NAME = aws_dynamodb_table.operations.name
    }
  }

  depends_on = [
    aws_iam_role_policy.receiver_operations,
    aws_iam_role_policy.processing_logs["receiver"],
  ]

  tags = var.tags
}

resource "aws_lambda_function" "worker" {
  function_name = "${var.function_name}-worker"
  role          = aws_iam_role.worker.arn

  package_type  = "Zip"
  runtime       = "python3.12"
  handler       = "dms_runtime.processing_lambda_function.stream_handler"
  architectures = ["x86_64"]

  s3_bucket         = var.artifact.bucket_name
  s3_key            = var.artifact.object_key
  s3_object_version = var.artifact.version_id
  source_code_hash  = var.artifact.source_code_hash

  publish     = true
  timeout     = 300
  memory_size = 1024

  environment {
    variables = {
      PROCESSING_OPERATIONS_TABLE_NAME = aws_dynamodb_table.operations.name
      PROCESSING_OPERATIONS_STREAM_ARN = aws_dynamodb_table.operations.stream_arn
      PROCESSING_RAW_BUCKET_OWNER      = var.input_bucket_owners.raw
      PROCESSING_MANIFEST_BUCKET_OWNER = var.input_bucket_owners.manifest
      PROCESSING_CONTRACT_BUCKET_OWNER = var.input_bucket_owners.contract
      PROCESSING_OUTPUT_BUCKET         = var.output.bucket_name
      PROCESSING_OUTPUT_BUCKET_OWNER   = var.output.bucket_owner
      PROCESSING_OUTPUT_PREFIX         = var.output.prefix
      PROCESSING_MAX_BATCH_BYTES       = tostring(var.max_batch_bytes)
    }
  }

  depends_on = [
    aws_iam_role_policy.worker_operations,
    aws_iam_role_policy.processing_logs["worker"],
    aws_iam_role_policy.worker_storage,
  ]

  tags = var.tags
}
