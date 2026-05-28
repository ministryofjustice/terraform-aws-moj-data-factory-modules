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

variable "client_id" {
  type        = string
  description = "OIDC audience/client ID to trust. Defaults to the Power BI Amazon S3 connector audience."
  default     = "https://analysis.windows.net/powerbi/connector/AmazonS3"

  validation {
    condition     = length(trimspace(var.client_id)) > 0
    error_message = "client_id must be a non-empty string."
  }
}

variable "thumbprint_list" {
  type        = list(string)
  description = "Optional override for OIDC provider SHA1 thumbprints. If null, the module derives the thumbprint from the issuer certificate chain."
  default     = null

  validation {
    condition = var.thumbprint_list == null || (
      length(var.thumbprint_list) > 0 &&
      alltrue([
        for thumbprint in var.thumbprint_list : can(regex("^[0-9a-fA-F]{40}$", trimspace(thumbprint)))
      ])
    )
    error_message = "thumbprint_list must be null or a non-empty list of 40-character SHA1 hex fingerprints."
  }
}
