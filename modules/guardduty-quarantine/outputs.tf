output "lambda_arn" {
  description = "ARN of the quarantine Lambda function."
  value       = module.quarantine_lambda.lambda_function_arn
}

output "lambda_name" {
  description = "Name of the quarantine Lambda function."
  value       = module.quarantine_lambda.lambda_function_name
}

output "lambda_role_arn" {
  description = "ARN of the IAM role used by the quarantine Lambda."
  value       = module.quarantine_lambda.lambda_role_arn
}

output "eventbridge_rule_arn" {
  description = "ARN of the EventBridge rule that triggers the quarantine Lambda."
  value       = aws_cloudwatch_event_rule.guardduty_quarantine.arn
}
