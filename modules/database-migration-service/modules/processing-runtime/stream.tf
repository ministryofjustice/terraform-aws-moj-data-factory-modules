resource "aws_lambda_event_source_mapping" "operations" {
  event_source_arn = aws_dynamodb_table.operations.stream_arn
  function_name    = aws_lambda_function.worker.qualified_arn

  starting_position       = "TRIM_HORIZON"
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]

  depends_on = [aws_iam_role_policy.worker_operations]
}
