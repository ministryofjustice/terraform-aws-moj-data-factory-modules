output "function_name" {
  description = "Name of the DMS runtime controller Lambda."
  value       = aws_lambda_function.runtime.function_name
}

output "function_arn" {
  description = "ARN of the DMS runtime controller Lambda."
  value       = aws_lambda_function.runtime.arn
}

output "execution_role_arn" {
  description = "ARN of the controller's execution role."
  value       = aws_iam_role.runtime.arn
}

output "log_group_name" {
  description = "CloudWatch log group used by the controller."
  value       = aws_cloudwatch_log_group.runtime.name
}
