output "function_name" {
  description = "Validation Lambda function name."
  value       = aws_lambda_function.validation.function_name
}

output "function_arn" {
  description = "Validation Lambda ARN."
  value       = aws_lambda_function.validation.arn
}

output "function_invoke_arn" {
  description = "Validation Lambda invocation ARN."
  value       = aws_lambda_function.validation.invoke_arn
}

output "execution_role_arn" {
  description = "Validation Lambda execution role ARN."
  value       = aws_iam_role.validation.arn
}

output "validation_table_name" {
  description = "File-validation audit table name."
  value       = aws_dynamodb_table.validation_audit.name
}

output "validation_table_arn" {
  description = "File-validation audit table ARN."
  value       = aws_dynamodb_table.validation_audit.arn
}

output "log_group_name" {
  description = "Validation CloudWatch log group name."
  value       = aws_cloudwatch_log_group.validation.name
}

output "failure_queue_arn" {
  description = "Asynchronous invocation failure destination ARN."
  value       = aws_sqs_queue.failures.arn
}

output "failure_queue_url" {
  description = "Queue URL used to inspect validation failure records."
  value       = aws_sqs_queue.failures.url
}

output "alarm_arns" {
  description = "Operational alarm ARNs."
  value = concat(
    [for alarm in aws_cloudwatch_metric_alarm.validation : alarm.arn],
    aws_cloudwatch_metric_alarm.failure_queue[*].arn,
  )
}
