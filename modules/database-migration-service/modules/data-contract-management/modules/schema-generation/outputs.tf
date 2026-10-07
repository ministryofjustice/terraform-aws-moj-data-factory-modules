output "function_name" {
  description = "Schema generation Lambda function name."
  value       = aws_lambda_function.generation.function_name
}

output "function_arn" {
  description = "Schema generation Lambda ARN."
  value       = aws_lambda_function.generation.arn
}

output "function_invoke_arn" {
  description = "Schema generation Lambda invocation ARN."
  value       = aws_lambda_function.generation.invoke_arn
}

output "execution_role_arn" {
  description = "Schema generation execution role ARN."
  value       = aws_iam_role.generation.arn
}

output "log_group_name" {
  description = "Schema generation CloudWatch log group name."
  value       = aws_cloudwatch_log_group.generation.name
}

output "failure_queue_arn" {
  description = "Asynchronous invocation failure destination ARN."
  value       = aws_sqs_queue.failures.arn
}

output "failure_queue_url" {
  description = "Queue URL used to inspect generation failure records."
  value       = aws_sqs_queue.failures.url
}

output "alarm_arns" {
  description = "Operational alarm ARNs."
  value = concat(
    [for alarm in aws_cloudwatch_metric_alarm.generation : alarm.arn],
    aws_cloudwatch_metric_alarm.failure_queue[*].arn,
  )
}
