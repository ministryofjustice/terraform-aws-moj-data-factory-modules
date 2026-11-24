provider "aws" {
  region = "eu-west-2"

  access_key = "test"
  secret_key = "test"

  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
}

override_data {
  target = data.aws_caller_identity.current

  values = {
    account_id = "111122223333"
  }
}

override_data {
  target = data.aws_region.current

  values = {
    region = "eu-west-2"
  }
}

override_data {
  target          = data.aws_partition.current
  override_during = plan

  values = {
    partition = "aws"
  }
}

override_resource {
  target          = aws_cloudwatch_log_group.runtime
  override_during = plan

  values = {
    arn = "arn:aws:logs:eu-west-2:111122223333:log-group:/aws/lambda/example-dms-runtime"
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

run "postgres_policy_contains_sync_permission" {
  command = plan

  assert {
    condition = length([
      for statement in jsondecode(data.aws_iam_policy_document.runtime.json).Statement :
      statement if statement.Sid == "SynchroniseSourceCredentials"
    ]) == 1

    error_message = "The PostgreSQL policy must include the credential-sync permission."
  }

  assert {
    condition = length([
      for statement in jsondecode(data.aws_iam_policy_document.runtime.json).Statement :
      statement if statement.Sid == "ReadPublishedDocumentVersions"
    ]) == 1

    error_message = "The policy must include access to published document versions."
  }

  assert {
    condition = length([
      for statement in jsondecode(data.aws_iam_policy_document.runtime.json).Statement :
      statement if statement.Sid == "InspectAndControlTasks"
    ]) == 1

    error_message = "The policy must include the scoped task operations."
  }

  assert {
    condition = try(
      toset(flatten([
        one([
          for statement in jsondecode(data.aws_iam_policy_document.runtime.json).Statement :
          statement if statement.Sid == "InspectAndControlTasks"
        ]).Resource
        ])) == toset([
        "arn:aws:dms:eu-west-2:111122223333:task:TASKONE"
      ]),
      false
    )

    error_message = "Task operations must be limited to the configured task ARN."
  }

  assert {
    condition = try(
      toset(flatten([
        one([
          for statement in jsondecode(data.aws_iam_policy_document.runtime.json).Statement :
          statement if statement.Sid == "ReadPublishedDocumentVersions"
        ]).Action
        ])) == toset([
        "s3:GetObjectVersion"
      ]) &&
      toset(flatten([
        one([
          for statement in jsondecode(data.aws_iam_policy_document.runtime.json).Statement :
          statement if statement.Sid == "ReadPublishedDocumentVersions"
        ]).Resource
        ])) == toset([
        "arn:aws:s3:::example-config/services/sandbox-postgres/*"
      ]),
      false
    )

    error_message = "Document access must be limited to versioned reads under the configured prefix."
  }

  assert {
    condition = try(
      toset(flatten([
        one([
          for statement in jsondecode(data.aws_iam_policy_document.runtime.json).Statement :
          statement if statement.Sid == "ReadAndUpdateRegistry"
        ]).Resource
        ])) == toset([
        "arn:aws:dynamodb:eu-west-2:111122223333:table/example-ingestion-registry"
      ]) &&
      toset(flatten([
        one([
          for statement in jsondecode(data.aws_iam_policy_document.runtime.json).Statement :
          statement if statement.Sid == "ReadAndUpdateRegistry"
        ]).Action
        ])) == toset([
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:ConditionCheckItem"
      ]),
      false
    )

    error_message = "Registry access must use the expected actions on the configured table."
  }

  assert {
    condition = try(
      toset(flatten([
        one([
          for statement in jsondecode(data.aws_iam_policy_document.runtime.json).Statement :
          statement if statement.Sid == "SynchroniseSourceCredentials"
        ]).Action
        ])) == toset([
        "lambda:InvokeFunction"
      ]) &&
      toset(flatten([
        one([
          for statement in jsondecode(data.aws_iam_policy_document.runtime.json).Statement :
          statement if statement.Sid == "SynchroniseSourceCredentials"
        ]).Resource
        ])) == toset([
        "arn:aws:lambda:eu-west-2:111122223333:function:credential-sync"
      ]),
      false
    )

    error_message = "Credential synchronisation must be limited to the configured Lambda."
  }
}

run "oracle_policy_omits_sync_permission" {
  command = plan

  variables {
    source_credentials = {
      mode = "connection_test"
    }
  }

  assert {
    condition = length([
      for statement in jsondecode(data.aws_iam_policy_document.runtime.json).Statement :
      statement if statement.Sid == "SynchroniseSourceCredentials"
    ]) == 0

    error_message = "The Oracle policy must not include credential-sync permission."
  }
}

run "raw_batch_handoff_permissions" {
  command = plan

  variables {
    raw_batch_handoff = {
      manifest = {
        bucket_name = "example-evidence"
        prefix      = "raw-batches/"
        kms_key_arn = "arn:aws:kms:eu-west-2:111122223333:key/11111111-1111-1111-1111-111111111111"
      }
      landings = [{
        bucket_name  = "example-raw"
        prefix       = "landing/v1/"
        kms_key_arns = ["arn:aws:kms:eu-west-2:111122223333:key/22222222-2222-2222-2222-222222222222"]
      }]
    }
  }

  assert {
    condition = (
      jsondecode(
        aws_lambda_function.runtime.environment[0].variables.RAW_BATCH_HANDOFF_CONFIG
      ).manifest_location.prefix == "raw-batches/"
    )

    error_message = "The Lambda must receive the configured manifest prefix."
  }

  assert {
    condition = one([
      for statement in jsondecode(
        data.aws_iam_policy_document.runtime.json
      ).Statement : statement.Resource
      if try(statement.Sid, "") == "ReadAndWriteRawBatchManifests"
    ]) == "arn:aws:s3:::example-evidence/raw-batches/*"

    error_message = "Manifest access must be limited to its configured prefix."
  }
}

run "reject_overlapping_raw_batch_prefixes" {
  command = plan

  variables {
    raw_batch_handoff = {
      manifest = {
        bucket_name = "example-raw"
        prefix      = "landing/"
      }
      landings = [{
        bucket_name = "example-raw"
        prefix      = "landing/v1/"
      }]
    }
  }

  expect_failures = [var.raw_batch_handoff]
}
