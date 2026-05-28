variable "tenant_id" {
  type        = string
  description = "Microsoft Entra tenant ID used in OIDC provider URL."
}

variable "oidc_provider_name" {
  type        = string
  description = "Tag name for the AWS IAM OIDC provider."
}
