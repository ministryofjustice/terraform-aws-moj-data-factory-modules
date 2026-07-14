locals {
  aws_account_id = data.aws_caller_identity.current.account_id

  bucket_name = "${var.bucket_prefix}-${local.aws_account_id}"

  common_tags = merge(
    var.tags,
    {
      ManagedBy = "Terraform"
      Module    = "s3-raw-bucket"
      Region    = data.aws_region.current.region
    }
  )
}