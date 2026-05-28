variable "tenant_id" {
  type        = string
  description = "Microsoft Entra tenant ID used in IAM trust policy conditions."
}

variable "object_id" {
  type        = string
  description = "Microsoft Entra object ID allowed to assume the IAM role."
}

variable "oidc_provider_arn" {
  type        = string
  description = "ARN of the IAM OIDC provider trusted by this role."
}

variable "bucket_arn" {
  type        = string
  description = "ARN of the S3 bucket this role can read."
}

variable "role_name" {
  type        = string
  description = "IAM role name for CFE Fabric web identity access."
}

variable "role_policy_name" {
  type        = string
  description = "Inline IAM policy name attached to the CFE Fabric role."
}
