module "oidc_provider" {
  source = "../../modules/fabric_connector/oidc-provider"

  tenant_id          = var.tenant_id
  oidc_provider_name = var.oidc_provider_name
  client_id          = var.client_id
}

module "iam_role" {
  source = "../../modules/fabric_connector/iam-role"

  object_id                          = var.object_id
  oidc_provider_arn                  = module.oidc_provider.arn
  oidc_provider_condition_key_prefix = module.oidc_provider.condition_key_prefix
  audience                           = module.oidc_provider.client_id
  bucket_arn                         = var.bucket_arn
  role_name                          = var.role_name
  role_policy_name                   = var.role_policy_name
}
