output "role_name" {
  description = "Name of the IAM role"
  value       = aws_iam_role.main.name
}

output "role_arn" {
  description = "ARN of the IAM role"
  value       = aws_iam_role.main.arn
}

output "role_unique_id" {
  description = "Stable and unique string identifying the IAM role"
  value       = aws_iam_role.main.unique_id
}
