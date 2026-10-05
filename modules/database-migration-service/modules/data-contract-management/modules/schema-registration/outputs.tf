output "function_name" {
  description = "Registration Lambda function name."
  value       = aws_lambda_function.registration.function_name
}

output "function_arn" {
  description = "Registration Lambda ARN."
  value       = aws_lambda_function.registration.arn
}

output "function_invoke_arn" {
  description = "Registration Lambda invocation ARN."
  value       = aws_lambda_function.registration.invoke_arn
}

output "execution_role_arn" {
  description = "Registration Lambda execution role ARN."
  value       = aws_iam_role.registration.arn
}

output "contract_table_name" {
  description = "Contract registration table name."
  value       = aws_dynamodb_table.contracts.name
}

output "contract_table_arn" {
  description = "Contract registration table ARN."
  value       = aws_dynamodb_table.contracts.arn
}

output "log_group_name" {
  description = "Registration CloudWatch log group name."
  value       = aws_cloudwatch_log_group.registration.name
}

output "failure_queue_arn" {
  description = "Asynchronous invocation failure destination ARN."
  value       = aws_sqs_queue.failures.arn
}

output "failure_queue_url" {
  description = "Queue URL used to inspect registration failure records."
  value       = aws_sqs_queue.failures.url
}

output "alarm_arns" {
  description = "Operational alarm ARNs."
  value = concat(
    [for alarm in aws_cloudwatch_metric_alarm.registration : alarm.arn],
    aws_cloudwatch_metric_alarm.failure_queue[*].arn,
  )
}