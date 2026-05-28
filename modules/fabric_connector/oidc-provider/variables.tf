variable "tenant_id" {
  type        = string
  description = "Microsoft Entra tenant ID used in OIDC provider URL."

  validation {
    condition     = length(trimspace(var.tenant_id)) > 0 && can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.tenant_id))
    error_message = "tenant_id must be a non-empty Microsoft Entra tenant ID in UUID format."
  }
}

variable "oidc_provider_name" {
  type        = string
  description = "Tag name for the AWS IAM OIDC provider."

  validation {
    condition     = length(trimspace(var.oidc_provider_name)) > 0 && can(regex("^[A-Za-z0-9._-]+$", var.oidc_provider_name))
    error_message = "oidc_provider_name must be non-empty and contain only letters, numbers, periods, underscores, and hyphens."
  }
}
