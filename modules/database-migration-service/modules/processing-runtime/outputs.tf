output "qualified_function_arn" {
  description = "Published Processing receiver ARN for submit and lookup requests."
  value       = aws_lambda_function.receiver.qualified_arn
}
