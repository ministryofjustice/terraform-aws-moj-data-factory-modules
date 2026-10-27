mock_provider "aws" {
  mock_data "aws_region" {
    defaults = {
      name   = "eu-west-2"
      region = "eu-west-2"
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

run "two_services_use_the_same_composition" {
  command = plan

  variables {
    compiled_artifact = jsondecode(
      file("fixtures/postgres.json")
    )

    compiled_artifacts = {
      postgres = jsondecode(file("fixtures/postgres.json"))
      oracle   = jsondecode(file("fixtures/oracle.json"))
    }
  }

  assert {
    condition = (
      toset(keys(module.service_ingestion))
      == toset(["postgres", "oracle"])
    )

    error_message = "Both services must use the same service-ingestion composition."
  }

  assert {
    condition = (
      var.compiled_artifacts.postgres.runtime_configuration.source_endpoint.engine
      == "postgres"
      &&
      var.compiled_artifacts.oracle.runtime_configuration.source_endpoint.engine
      == "oracle"
    )

    error_message = "The fixtures must contain PostgreSQL and Oracle services."
  }

  assert {
    condition = alltrue(flatten([
      for service_name, artifact in var.compiled_artifacts : [
        for group_name, inputs in artifact.module_inputs : (
          module.service_ingestion[service_name].replication_tasks[group_name][group_name].id
          == inputs.replication_tasks[group_name].replication_task_id
          &&
          module.service_ingestion[service_name].replication_tasks[group_name][group_name].migration_type
          == inputs.replication_tasks[group_name].migration_type
        )
      ]
    ]))

    error_message = "Each service must retain its compiled task identities and migration modes."
  }

  assert {
    condition = alltrue(flatten([
      for service_name, artifact in var.compiled_artifacts : [
        for group_name, inputs in artifact.module_inputs : (
          module.service_ingestion[service_name].deployment_bindings[group_name].deployment_name
          == inputs.name
          &&
          module.service_ingestion[service_name].deployment_bindings[group_name].source_endpoint.engine
          == inputs.source_endpoint.engine
          &&
          module.service_ingestion[service_name].deployment_bindings[group_name].target_endpoint.bucket_folder
          == inputs.target.bucket_folder
          &&
          jsonencode(
            module.service_ingestion[service_name].deployment_bindings[group_name].replication_tasks[group_name].table_mappings
          )
          == jsonencode(
            jsondecode(inputs.replication_tasks[group_name].table_mappings)
          )
        )
      ]
    ]))

    error_message = "Each deployment binding must retain the compiled source, landing path and exact table mappings."
  }

  assert {
    condition = (
      module.service_ingestion["postgres"].configuration_revision
      == var.compiled_artifacts.postgres.configuration_revision
      &&
      module.service_ingestion["oracle"].configuration_revision
      == var.compiled_artifacts.oracle.configuration_revision
      &&
      module.service_ingestion["postgres"].artifact_revision
      == var.compiled_artifacts.postgres.artifact_revision
      &&
      module.service_ingestion["oracle"].artifact_revision
      == var.compiled_artifacts.oracle.artifact_revision
    )

    error_message = "Each service must retain its own configuration and artifact revisions."
  }

  assert {
    condition = (
      var.compiled_artifacts.postgres.module_inputs.integration.name
      != var.compiled_artifacts.oracle.module_inputs.integration.name
      &&
      var.compiled_artifacts.postgres.module_inputs.integration.target.bucket_folder
      != var.compiled_artifacts.oracle.module_inputs.integration.target.bucket_folder
    )

    error_message = "The two example services must use separate deployment names and landing paths."
  }
}

run "reject_non_development_artifact" {
  command = plan

  module {
    source = "../../../modules/service-ingestion"
  }

  variables {
    compiled_artifact = merge(
      jsondecode(file("fixtures/postgres.json")),
      {
        runtime_configuration = merge(
          jsondecode(file("fixtures/postgres.json")).runtime_configuration,
          {
            environment = "production"
          }
        )
      }
    )
  }

  expect_failures = [
    var.compiled_artifact,
  ]
}
