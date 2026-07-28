locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region

  tags = {
    ManagedBy   = "Terraform"
    Region      = local.region
    AccountId   = local.account_id
    AccountName = terraform.workspace
  }
}
