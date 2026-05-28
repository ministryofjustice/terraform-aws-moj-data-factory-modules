output "oidc_provider_arn" {
  description = "Created IAM OIDC provider ARN."
  value       = module.oidc_provider.arn
}

output "oidc_issuer" {
  description = "OIDC provider issuer URL."
  value       = module.oidc_provider.issuer
}

output "iam_role_arn" {
  description = "Created IAM role ARN used by Fabric."
  value       = module.iam_role.arn
}
