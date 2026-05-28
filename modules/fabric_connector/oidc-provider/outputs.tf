output "arn" {
  description = "ARN of the IAM OIDC provider."
  value       = aws_iam_openid_connect_provider.this.arn
}
