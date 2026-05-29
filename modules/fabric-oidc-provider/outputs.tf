output "arn" {
  description = "ARN of the IAM OIDC provider."
  value       = aws_iam_openid_connect_provider.this.arn
}

output "issuer" {
  description = "The OIDC provider issuer URL."
  value       = aws_iam_openid_connect_provider.this.url
}

output "condition_key_prefix" {
  description = "Prefix for IAM condition keys derived from the issuer (used for trust policy conditions)."
  value       = "${replace(aws_iam_openid_connect_provider.this.url, "https://", "")}:"
}

output "client_id" {
  description = "Configured OIDC audience/client ID."
  value       = var.client_id
}
