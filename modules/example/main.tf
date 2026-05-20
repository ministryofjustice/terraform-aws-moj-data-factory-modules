locals {
  default_tags = {
    Name        = var.name
    Environment = var.environment
    ManagedBy   = "terraform"
    Module      = "example"
  }

  tags = merge(local.default_tags, var.additional_tags)
}
