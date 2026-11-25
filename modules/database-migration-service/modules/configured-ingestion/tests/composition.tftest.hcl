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

run "raw_history_is_disabled_by_default" {
  command = plan

  assert {
    condition     = length(output.raw_history_runtimes) == 0
    error_message = "Existing services must not deploy Raw History unless explicitly configured."
  }
}

run "raw_history_is_service_scoped" {
  command = plan

  variables {
    raw_history_runtimes = jsondecode(
      file("tests/fixtures/raw-history-runtimes.json")
    )
  }

  override_module {
    target = module.raw_history["sandbox-postgres"]

    outputs = {
      function_arn = "arn:aws:lambda:eu-west-2:111122223333:function:sandbox-sandbox-postgres-raw-history"

      qualified_function_arn = "arn:aws:lambda:eu-west-2:111122223333:function:sandbox-sandbox-postgres-raw-history:7"

      function_version = "7"

      execution_role_arn = "arn:aws:iam::111122223333:role/example-raw-history"

      operations_table = {
        table_name = "sandbox-sandbox-postgres-raw-history-operations"
        table_arn  = "arn:aws:dynamodb:eu-west-2:111122223333:table/sandbox-sandbox-postgres-raw-history-operations"
      }

      configuration_reference = {
        bucket_name = "example-config"
        object_key  = "components/raw-history/sandbox-postgres/example.json"
        version_id  = "configuration-version-one"
        sha256      = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      }
    }
  }

  assert {
    condition = (
      toset(keys(output.raw_history_runtimes)) ==
      toset(["sandbox-postgres"])
    )

    error_message = "Only the explicitly configured service must receive a Raw History deployment."
  }

  assert {
    condition = (
      output.raw_history_runtimes["sandbox-postgres"].function_name ==
      "sandbox-sandbox-postgres-raw-history"
    )

    error_message = "The receiver name must be service-scoped rather than extraction-generation-scoped."
  }

  assert {
    condition = (
      output.raw_history_runtimes["sandbox-postgres"].component_targets["standard"].kind == "lambda" &&
      output.raw_history_runtimes["sandbox-postgres"].component_targets["standard"].arn ==
      "arn:aws:lambda:eu-west-2:111122223333:function:sandbox-sandbox-postgres-raw-history:7"
    )

    error_message = "The component target must reference the published Lambda version."
  }

  assert {
    condition     = length(output.deployments) == 3
    error_message = "Adding Raw History must retain both PostgreSQL extraction generations and the Oracle deployment."
  }
}

run "reject_raw_history_for_unknown_service" {
  command = plan

  variables {
    raw_history_runtimes = {
      for source, settings in jsondecode(
        file("tests/fixtures/raw-history-runtimes.json")
      ) :
      "unknown-service" => settings
    }
  }

  expect_failures = [
    var.raw_history_runtimes,
  ]
}

run "reject_history_profile_missing_from_yaml" {
  command = plan

  variables {
    raw_history_runtimes = {
      for source, settings in jsondecode(
        file("tests/fixtures/raw-history-runtimes.json")
      ) :
      source => merge(settings, {
        tables = [
          for table in settings.tables :
          merge(table, {
            profile = "not-declared-in-yaml"
          })
        ]
      })
    }
  }

  expect_failures = [
    terraform_data.raw_history_contract["sandbox-postgres"],
  ]
}
