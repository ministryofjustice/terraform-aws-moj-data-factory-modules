locals { 

  bucket_name = "${var.bucket_prefix}-${data.aws_caller_identity.current.account_id}-${data.aws_region.current.region}-an"

  common_tags = merge(
    var.tags,
    {
      ManagedBy = "Terraform"
      Region    = data.aws_region.current.region
      AccountId   = data.aws_caller_identity.current.account_id
      AccountName = terraform.workspace
    }
  )
}