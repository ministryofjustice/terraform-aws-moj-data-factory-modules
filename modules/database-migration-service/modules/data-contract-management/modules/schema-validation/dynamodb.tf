resource "aws_dynamodb_table" "validation_audit" {
  name         = local.validation_table_name
  billing_mode = "PAY_PER_REQUEST"

  hash_key = "validation_id"

  deletion_protection_enabled = var.table_deletion_protection_enabled

  attribute {
    name = "validation_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.validation_table_kms_key_arn
  }

  tags = var.tags

  lifecycle {
    precondition {
      condition = (
        var.registry_table_name ==
        local.registry_table_name_from_arn
      )
      error_message = "registry_table_name must match the table name in registry_table_arn."
    }

    precondition {
      condition = (
        local.validation_table_name !=
        var.registry_table_name
      )
      error_message = "The validation audit table must be separate from the contract registry."
    }
  }
}
