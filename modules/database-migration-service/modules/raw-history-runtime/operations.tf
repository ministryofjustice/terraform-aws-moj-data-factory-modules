resource "aws_dynamodb_table" "operations" {
  name         = local.operations_table_name
  billing_mode = "PAY_PER_REQUEST"

  hash_key = "operation_id"

  deletion_protection_enabled = var.deletion_protection_enabled

  attribute {
    name = "operation_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.operations_kms_key_arn
  }

  tags = var.tags

  depends_on = [
    terraform_data.configuration_contract,
  ]
}
