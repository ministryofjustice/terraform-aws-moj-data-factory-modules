module "kms" {
  source  = "terraform-aws-modules/kms/aws"
  version = "~> 2.0"

  description             = var.description
  enable_key_rotation     = var.enable_key_rotation
  deletion_window_in_days = var.deletion_window_in_days
  is_enabled              = var.is_enabled
  multi_region            = var.multi_region

  key_service_users  = var.key_service_users
  key_administrators = var.key_administrators

  aliases = var.aliases
  tags    = var.tags
}

