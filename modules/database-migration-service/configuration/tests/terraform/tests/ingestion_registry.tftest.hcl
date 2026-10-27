mock_provider "aws" {}

run "registry_table_configuration" {
  command = plan

  module {
    source = "../../../modules/ingestion-registry"
  }

  variables {
    name        = "sandbox-ingestion-registry"
    kms_key_arn = "arn:aws:kms:eu-west-2:111122223333:key/11111111-1111-1111-1111-111111111111"

    tags = {
      Environment = "development"
    }
  }

  assert {
    condition = (
      aws_dynamodb_table.this.hash_key == "pk"
      &&
      aws_dynamodb_table.this.range_key == "sk"
      &&
      aws_dynamodb_table.this.billing_mode == "PAY_PER_REQUEST"
    )

    error_message = "The registry must use the expected keys and on-demand billing."
  }

  assert {
    condition = (
      aws_dynamodb_table.this.deletion_protection_enabled
      &&
      aws_dynamodb_table.this.point_in_time_recovery[0].enabled
    )

    error_message = "The registry must be protected and have point-in-time recovery enabled."
  }

  assert {
    condition = (
      aws_dynamodb_table.this.server_side_encryption[0].enabled
      &&
      aws_dynamodb_table.this.server_side_encryption[0].kms_key_arn
      == var.kms_key_arn
    )

    error_message = "The registry must use the supplied KMS key."
  }

  assert {
    condition     = output.table_name == var.name
    error_message = "The module must expose the configured table name."
  }
}

run "reject_invalid_table_name" {
  command = plan

  module {
    source = "../../../modules/ingestion-registry"
  }

  variables {
    name        = "x"
    kms_key_arn = "arn:aws:kms:eu-west-2:111122223333:key/11111111-1111-1111-1111-111111111111"
  }

  expect_failures = [
    var.name,
  ]
}
