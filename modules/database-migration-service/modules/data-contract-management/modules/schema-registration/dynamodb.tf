resource "aws_dynamodb_table" "contracts" {
  name         = "${var.name}-contracts"
  billing_mode = "PAY_PER_REQUEST"

  hash_key  = "contract_id"
  range_key = "contract_version"

  deletion_protection_enabled = var.table_deletion_protection_enabled

  attribute {
    name = "contract_id"
    type = "S"
  }

  attribute {
    name = "contract_version"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.table_kms_key_arn
  }

  tags = var.tags
}