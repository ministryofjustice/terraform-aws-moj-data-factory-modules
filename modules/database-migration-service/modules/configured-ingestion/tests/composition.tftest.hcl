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
  name_prefix        = "sandbox"
  compiled_artifacts = jsondecode(file("tests/fixtures/services.json"))

  artifact = {
    bucket_name      = "example-runtime-artifacts"
    object_key       = "lambda/dms-runtime-controller/example/package.zip"
    version_id       = "version-one"
    source_code_hash = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
  }

  registry = {
    table_name = "example-ingestion-registry"
    table_arn  = "arn:aws:dynamodb:eu-west-2:111122223333:table/example-ingestion-registry"
  }

  replication_capacities = {
    shared = {
      vpc_id                   = "vpc-0123456789abcdef0"
      replication_instance_arn = "arn:aws:dms:eu-west-2:111122223333:rep:SHAREDPOOL"
      replication_instance_id  = "sandbox-shared"

      replication_security_group_id = "sg-0123456789abcdef0"

      replication_security_group_ids = [
        "sg-0123456789abcdef0",
      ]

      replication_subnet_group_id = "sandbox-shared-subnets"

      task_log_group = {
        name = "dms-tasks-sandbox-shared"
        arn  = "arn:aws:logs:eu-west-2:111122223333:log-group:dms-tasks-sandbox-shared"
      }
    }
  }

  capacity_assignments = {
    "sandbox-postgres/sandbox-v1" = {
      integration = "shared"
    }

    "sandbox-postgres/sandbox-v2" = {
      integration = "shared"
    }

    "sandbox-oracle/sandbox-v1" = {
      integration = "shared"
    }
  }

  runtime_settings = {
    sandbox-postgres = {
      published_documents = {
        bucket_name  = "example-config"
        prefix       = "services/sandbox-postgres/"
        bucket_owner = "111122223333"
      }

      source_credentials = {
        mode              = "synchronise_then_test"
        sync_function_arn = "arn:aws:lambda:eu-west-2:111122223333:function:credential-sync"
      }
    }

    sandbox-oracle = {
      published_documents = {
        bucket_name  = "example-config"
        prefix       = "services/sandbox-oracle/"
        bucket_owner = "111122223333"
      }

      source_credentials = {
        mode = "connection_test"
      }
    }
  }
}

run "two_services_and_retained_generations_share_capacity" {
  command = plan

  assert {
    condition     = length(output.deployments) == 3
    error_message = "Both PostgreSQL generations and the Oracle deployment must be retained."
  }

  assert {
    condition = (
      output.deployments["sandbox-postgres/sandbox-v1"].runtime.function_name
      == output.deployments["sandbox-postgres/sandbox-v2"].runtime.function_name
    )

    error_message = "Runtime identity must remain service-scoped across prepared generations."
  }

  assert {
    condition = alltrue([
      for deployment in values(output.deployments) :
      deployment.deployment_bindings.integration.replication_instance_arn
      == "arn:aws:dms:eu-west-2:111122223333:rep:SHAREDPOOL"
    ])

    error_message = "All configured task groups must use the assigned platform capacity."
  }

  assert {
    condition = (
      output.deployments["sandbox-postgres/sandbox-v1"].deployment_bindings.integration.target_endpoint.bucket_folder
      != output.deployments["sandbox-postgres/sandbox-v2"].deployment_bindings.integration.target_endpoint.bucket_folder
    )

    error_message = "Extraction generations must have separate landing boundaries."
  }
}

run "reject_missing_runtime_settings" {
  command = plan

  variables {
    runtime_settings = {}
  }

  expect_failures = [
    var.runtime_settings,
  ]
}

run "reject_non_development" {
  command = plan

  variables {
    compiled_artifacts = {
      for key, artifact in jsondecode(file("tests/fixtures/services.json")) :
      key => merge(artifact, {
        runtime_configuration = merge(
          artifact.runtime_configuration,
          {
            environment = "production"
          }
        )
      })
    }
  }

  expect_failures = [
    var.compiled_artifacts,
  ]
}

run "reject_unassigned_generation" {
  command = plan

  variables {
    capacity_assignments = {}
  }

  expect_failures = [
    var.capacity_assignments,
  ]
}
