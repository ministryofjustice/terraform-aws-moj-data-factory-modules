variable "aws_region" {
  description = "AWS region where resources are created."
  type        = string
}

variable "tenant_id" {
  description = "Microsoft Entra tenant ID (UUID)."
  type        = string
}

variable "object_id" {
  description = "Microsoft Entra object ID (UUID) for the service principal allowed to assume the IAM role."
  type        = string
}

variable "bucket_arn" {
  description = "ARN of the existing S3 bucket Fabric should read from."
  type        = string
}

variable "oidc_provider_name" {
  description = "Name tag used on the OIDC provider."
  type        = string
  default     = "fabric-powerbi-oidc"
}

variable "role_name" {
  description = "IAM role name for Fabric S3 access."
  type        = string
  default     = "fabric-s3-access"
}

variable "role_policy_name" {
  description = "Inline policy name attached to the IAM role."
  type        = string
  default     = "fabric-s3-read-policy"
}

variable "client_id" {
  description = "OIDC audience/client ID. Keep default for Power BI Amazon S3 connector unless you have a different audience."
  type        = string
  default     = "https://analysis.windows.net/powerbi/connector/AmazonS3"
}
