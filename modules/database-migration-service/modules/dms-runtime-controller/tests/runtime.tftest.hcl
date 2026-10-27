mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = jsonencode({
        Version = "2012-10-17"
        Statement = [
          {
            Effect = "Allow"
            Action = "sts:AssumeRole"
            Principal = {
              Service = "lambda.amazonaws.com"
            }
          }
        ]
      })
    }
  }

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
}

variables {
  function_name = "example-dms-runtime"

  service_context = {
    environment   = "development"
    account_id    = "111122223333"
    region        = "eu-west-2"
    source        = "sandbox-postgres"
    database_name = "example"
  }

  artifact = {
    bucket_name      = "example-runtime-artifacts"
    object_key       = "dms-runtime/example.zip"
    version_id       = "artifact-version-one"
    source_code_hash = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
  }

  registry = {
    table_name = "example-ingestion-registry"
    table_arn  = "arn:aws:dynamodb:eu-west-2:111122223333:table/example-ingestion-registry"
  }

  published_documents = {
    bucket_name  = "example-config"
    prefix       = "services/sandbox-postgres/"
    bucket_owner = "111122223333"
  }

  dms_resources = {
    replication_task_arns = [
      "arn:aws:dms:eu-west-2:111122223333:task:TASKONE",
    ]

    replication_instance_arns = [
      "arn:aws:dms:eu-west-2:111122223333:rep:INSTANCEONE",
    ]

    source_endpoint_arns = [
      "arn:aws:dms:eu-west-2:111122223333:endpoint:SOURCEONE",
    ]
  }

  source_credentials = {
    mode              = "synchronise_then_test"
    sync_function_arn = "arn:aws:lambda:eu-west-2:111122223333:function:credential-sync"
  }
}

run "postgres_controller_uses_sync_function" {
  command = plan

  assert {
    condition = (
      aws_lambda_function.runtime.environment[0].variables["SOURCE_CREDENTIAL_MODE"]
      == "synchronise_then_test"
    )

    error_message = "PostgreSQL controller must use the configured credential mode."
  }

  assert {
    condition = (
      aws_lambda_function.runtime.environment[0].variables["CREDENTIAL_SYNC_FUNCTION_ARN"]
      == "arn:aws:lambda:eu-west-2:111122223333:function:credential-sync"
    )

    error_message = "The configured credential-sync ARN must be passed to the Lambda."
  }

  assert {
    condition = (
      aws_lambda_function.runtime.environment[0].variables["RUNTIME_SOURCE"]
      == "sandbox-postgres"
    )

    error_message = "The Lambda must receive the authorised source."
  }

  assert {
    condition = (
      aws_lambda_function.runtime.s3_object_version
      == "artifact-version-one"
    )

    error_message = "The Lambda must use the specified artifact version."
  }
}

run "oracle_controller_omits_sync_function" {
  command = plan

  variables {
    service_context = {
      environment   = "development"
      account_id    = "111122223333"
      region        = "eu-west-2"
      source        = "sandbox-oracle"
      database_name = "example"
    }

    source_credentials = {
      mode = "connection_test"
    }
  }

  assert {
    condition = (
      aws_lambda_function.runtime.environment[0].variables["SOURCE_CREDENTIAL_MODE"]
      == "connection_test"
    )

    error_message = "Oracle controller must use connection_test."
  }

  assert {
    condition = !contains(
      keys(aws_lambda_function.runtime.environment[0].variables),
      "CREDENTIAL_SYNC_FUNCTION_ARN"
    )

    error_message = "connection_test must omit the sync function setting."
  }
}

run "reject_account_mismatch" {
  command = plan

  variables {
    service_context = {
      environment   = "development"
      account_id    = "999900001111"
      region        = "eu-west-2"
      source        = "sandbox-postgres"
      database_name = "example"
    }
  }

  expect_failures = [
    aws_lambda_function.runtime,
  ]
}

run "reject_task_in_another_account" {
  command = plan

  variables {
    dms_resources = {
      replication_task_arns = [
        "arn:aws:dms:eu-west-2:999900001111:task:TASKONE",
      ]

      replication_instance_arns = [
        "arn:aws:dms:eu-west-2:111122223333:rep:INSTANCEONE",
      ]

      source_endpoint_arns = [
        "arn:aws:dms:eu-west-2:111122223333:endpoint:SOURCEONE",
      ]
    }
  }

  expect_failures = [
    aws_lambda_function.runtime,
  ]
}

run "reject_wildcard_in_document_prefix" {
  command = plan

  variables {
    published_documents = {
      bucket_name  = "example-config"
      prefix       = "services/*/"
      bucket_owner = "111122223333"
    }
  }

  expect_failures = [
    aws_lambda_function.runtime,
  ]
}
