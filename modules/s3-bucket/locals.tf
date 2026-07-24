locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region

  bucket_name = "${var.bucket_prefix}-${local.account_id}-${local.region}-an"

  common_tags = merge(
    var.tags,
    {
      ManagedBy   = "Terraform"
      Region      = local.region
      AccountId   = local.account_id
      AccountName = terraform.workspace
    }
  )
}
