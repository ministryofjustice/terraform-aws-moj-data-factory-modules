mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "111122223333"
    }
  }

  mock_data "aws_region" {
    defaults = {
      region = "eu-west-2"
    }
  }

  mock_data "aws_partition" {
    defaults = {
      partition = "aws"
    }
  }

  mock_data "aws_iam_policy_document" {
    defaults = {
      json = jsonencode({
        Version   = "2012-10-17"
        Statement = []
      })
    }
  }
}

variables {
  function_name = "example-raw-history"

  service_context = {
    environment   = "development"
    account_id    = "111122223333"
    region        = "eu-west-2"
    source        = "example-source"
    database_name = "example_database"
  }

  registry = {
    table_name = "example-ingestion-registry"
    table_arn  = "arn:aws:dynamodb:eu-west-2:111122223333:table/example-ingestion-registry"
  }

  configuration_store = {
    bucket_name  = "example-ingestion-config"
    bucket_owner = "111122223333"
    prefix       = "components/raw-history/configuration/"
    kms_key_arn  = "arn:aws:kms:eu-west-2:111122223333:key/configuration-key"
  }

  published_documents = {
    bucket_name  = "example-ingestion-config"
    bucket_owner = "111122223333"
    prefix       = "services/example-source/"
    kms_key_arns = [
      "arn:aws:kms:eu-west-2:111122223333:key/configuration-key",
    ]
  }

  history_bucket = {
    bucket_name  = "example-raw-history"
    bucket_owner = "111122223333"
    kms_key_arn  = "arn:aws:kms:eu-west-2:111122223333:key/history-key"
  }

  operations_kms_key_arn = "arn:aws:kms:eu-west-2:111122223333:key/operations-key"

  tables = [
    {
      schema_name    = "public"
      table_name     = "example_table"
      profile        = "standard"
      history_prefix = "example-domain/example_table/"

      landings = [
        {
          bucket_name  = "example-raw-landing"
          bucket_owner = "111122223333"
          prefix       = "ingestion/example-source/generation-one/"
          kms_key_arns = [
            "arn:aws:kms:eu-west-2:111122223333:key/raw-key",
          ]
        },
      ]

      manifest_location = {
        bucket_name  = "example-ingestion-config"
        bucket_owner = "111122223333"
        prefix       = "raw-batches/example-source/"
      }

      contract_location = {
        bucket_name  = "example-contracts"
        bucket_owner = "111122223333"
        prefix       = "example-source/example_database/public/"
      }
    },
  ]

  artifact = {
    bucket_name      = "example-runtime-artifacts"
    object_key       = "lambda/runtime/test/package.zip"
    version_id       = "package-version-one"
    source_code_hash = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
  }

  registry_kms_key_arn = "arn:aws:kms:eu-west-2:111122223333:key/registry-key"
}

run "renders_receiver_configuration" {
  command = plan

  assert {
    condition = (
      output.configuration_document.configuration_version == 1 &&
      output.configuration_document.context.source == "example-source" &&
      output.configuration_document.operation_table_name == "example-raw-history-operations"
    )

    error_message = "The receiver configuration must contain its version, service context and separate operation table."
  }

  assert {
    condition = (
      output.configuration_document.tables[0].history_prefix == "example-domain/example_table/" &&
      output.configuration_document.tables[0].landings[0].prefix == "ingestion/example-source/generation-one/"
    )

    error_message = "The configuration must preserve the agreed history and Raw input locations."
  }

  assert {
    condition = (
      !contains(
        keys(output.configuration_document.tables[0].landings[0]),
        "kms_key_arns"
      )
    )

    error_message = "IAM-only KMS metadata must not leak into the receiver configuration schema."
  }

  assert {
    condition = (
      aws_dynamodb_table.operations.hash_key == "operation_id" &&
      aws_dynamodb_table.operations.point_in_time_recovery[0].enabled &&
      aws_dynamodb_table.operations.deletion_protection_enabled
    )

    error_message = "The operation table must use operation_id and retain its recovery protections."
  }
}

run "rejects_wildcard_configuration_prefix" {
  command = plan

  variables {
    configuration_store = {
      bucket_name  = "example-ingestion-config"
      bucket_owner = "111122223333"
      prefix       = "components/*/"
      kms_key_arn  = "arn:aws:kms:eu-west-2:111122223333:key/configuration-key"
    }
  }

  expect_failures = [
    terraform_data.configuration_contract,
  ]
}

run "rejects_configuration_in_another_account" {
  command = plan

  variables {
    configuration_store = {
      bucket_name  = "example-ingestion-config"
      bucket_owner = "999900001111"
      prefix       = "components/raw-history/configuration/"
      kms_key_arn  = "arn:aws:kms:eu-west-2:111122223333:key/configuration-key"
    }
  }

  expect_failures = [
    terraform_data.configuration_contract,
  ]
}

run "rejects_batch_above_backend_limit" {
  command = plan

  variables {
    max_batch_bytes = 64 * 1024 * 1024 + 1
  }

  expect_failures = [
    var.max_batch_bytes,
  ]
}

run "publishes_raw_history_receiver" {
  command = plan

  assert {
    condition = (
      aws_lambda_function.runtime.handler == "dms_runtime.raw_history_bootstrap.lambda_handler" &&
      aws_lambda_function.runtime.runtime == "python3.12" &&
      aws_lambda_function.runtime.publish
    )

    error_message = "The module must publish the Python Raw History receiver."
  }

  assert {
    condition = (
      aws_lambda_function.runtime.s3_object_version == "package-version-one" &&
      aws_lambda_function.runtime.source_code_hash == var.artifact.source_code_hash
    )

    error_message = "The receiver must use the pinned, tested runtime artifact."
  }

  assert {
    condition = (
      aws_lambda_function.runtime.environment[0].variables["RUNTIME_SOURCE"] == "example-source" &&
      aws_lambda_function.runtime.environment[0].variables["INGESTION_REGISTRY_TABLE_NAME"] == var.registry.table_name &&
      contains(
        keys(aws_lambda_function.runtime.environment[0].variables),
        "RAW_HISTORY_CONFIGURATION"
      )
    )

    error_message = "The receiver must receive its service scope and immutable configuration reference."
  }
}

run "registry_access_is_read_only" {
  command = plan

  assert {
    condition = (
      toset(one([
        for statement in data.aws_iam_policy_document.runtime.statement :
        statement
        if statement.sid == "ReadRegistry"
      ]).actions) == toset(["dynamodb:GetItem"])
    )

    error_message = "The Raw History receiver must only read the shared registry."
  }

  assert {
    condition = (
      toset(one([
        for statement in data.aws_iam_policy_document.runtime.statement :
        statement
        if statement.sid == "ReadRegistry"
      ]).resources) == toset([var.registry.table_arn])
    )

    error_message = "Registry reads must target only the configured shared table."
  }
}

run "history_access_does_not_allow_deletion" {
  command = plan

  assert {
    condition = (
      toset(one([
        for statement in data.aws_iam_policy_document.runtime.statement :
        statement
        if statement.sid == "ManageHistoryObjects"
        ]).actions) == toset([
        "s3:GetObject",
        "s3:GetObjectVersion",
        "s3:PutObject",
      ])
    )

    error_message = "History access must support commits and recovery without deleting objects."
  }

  assert {
    condition = (
      toset(one([
        for statement in data.aws_iam_policy_document.runtime.statement :
        statement
        if statement.sid == "ReadPinnedInputs"
      ]).actions) == toset(["s3:GetObjectVersion"])
    )

    error_message = "Raw inputs, contracts and manifests must be read through pinned object versions."
  }
}

run "supports_maximum_length_function_name" {
  command = plan

  variables {
    function_name = "raw-history-${join("", [for index in range(52) : "a"])}"
  }

  assert {
    condition = (
      length(var.function_name) == 64 &&
      length(aws_iam_role.runtime.name_prefix) <= 38 &&
      endswith(aws_iam_role.runtime.name_prefix, "-history-")
    )

    error_message = "A 64-character Lambda name must produce an IAM role prefix within the provider's 38-character limit."
  }
}

run "allows_missing_commit_detection_on_history_bucket" {
  command = plan

  assert {
    condition = (
      toset(one([
        for statement in data.aws_iam_policy_document.runtime.statement :
        statement
        if statement.sid == "CheckHistoryCommitExistence"
      ]).actions) == toset(["s3:ListBucket"])
    )

    error_message = "Commit lookup must have ListBucket permission to distinguish missing evidence from access denial."
  }

  assert {
    condition = (
      toset(one([
        for statement in data.aws_iam_policy_document.runtime.statement :
        statement
        if statement.sid == "CheckHistoryCommitExistence"
        ]).resources) == toset([
        "arn:aws:s3:::example-raw-history",
      ])
    )

    error_message = "Listing permission must be restricted to the configured history bucket."
  }
}
