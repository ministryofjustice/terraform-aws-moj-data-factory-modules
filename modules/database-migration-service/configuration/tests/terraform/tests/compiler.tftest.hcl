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

run "postgres_compiled_inputs" {
  command = plan

  variables {
    compiled_artifact = jsondecode(
      file("fixtures/postgres.json")
    )
  }

  assert {
    condition = (
      var.compiled_artifact.runtime_configuration.source_endpoint.engine
      == "postgres"
    )

    error_message = "The PostgreSQL fixture must contain PostgreSQL configuration."
  }

  assert {
    condition = alltrue([
      for group_name, inputs in var.compiled_artifact.module_inputs :
      module.source_ingestion[group_name].replication_tasks[group_name].id
      == inputs.replication_tasks[group_name].replication_task_id
    ])

    error_message = "The module must retain the compiler's task identifiers."
  }

  assert {
    condition = alltrue([
      for group_name, inputs in var.compiled_artifact.module_inputs :
      module.source_ingestion[group_name].replication_tasks[group_name].migration_type
      == inputs.replication_tasks[group_name].migration_type
    ])

    error_message = "The module must retain the compiler's ingestion modes."
  }

  assert {
    condition = alltrue([
      for group_name, inputs in var.compiled_artifact.module_inputs : (
        module.source_ingestion[group_name].deployment_binding.deployment_name
        == inputs.name
        &&
        module.source_ingestion[group_name].deployment_binding.environment
        == inputs.environment
        &&
        module.source_ingestion[group_name].deployment_binding.source_endpoint.engine
        == inputs.source_endpoint.engine
        &&
        module.source_ingestion[group_name].deployment_binding.source_endpoint.database_name
        == inputs.source_endpoint.database_name
        &&
        module.source_ingestion[group_name].deployment_binding.target_endpoint.bucket_name
        == inputs.target.bucket_name
        &&
        module.source_ingestion[group_name].deployment_binding.target_endpoint.bucket_folder
        == inputs.target.bucket_folder
        &&
        jsonencode(
          module.source_ingestion[group_name].deployment_binding.replication_tasks[group_name].table_mappings
        )
        == jsonencode(
          jsondecode(inputs.replication_tasks[group_name].table_mappings)
        )
      )
    ])

    error_message = "Deployment bindings must retain the compiled identity, source, landing boundary and exact table mappings."
  }
}

run "oracle_compiled_inputs" {
  command = plan

  variables {
    compiled_artifact = jsondecode(
      file("fixtures/oracle.json")
    )
  }

  assert {
    condition = (
      var.compiled_artifact.runtime_configuration.source_endpoint.engine
      == "oracle"
    )

    error_message = "The Oracle fixture must contain Oracle configuration."
  }

  assert {
    condition = alltrue([
      for group_name, inputs in var.compiled_artifact.module_inputs :
      module.source_ingestion[group_name].replication_tasks[group_name].id
      == inputs.replication_tasks[group_name].replication_task_id
    ])

    error_message = "The module must retain the compiler's task identifiers."
  }

  assert {
    condition = alltrue([
      for group_name, inputs in var.compiled_artifact.module_inputs :
      module.source_ingestion[group_name].replication_tasks[group_name].migration_type
      == inputs.replication_tasks[group_name].migration_type
    ])

    error_message = "The module must retain the compiler's ingestion modes."
  }

  assert {
    condition = alltrue([
      for group_name, inputs in var.compiled_artifact.module_inputs : (
        module.source_ingestion[group_name].deployment_binding.deployment_name
        == inputs.name
        &&
        module.source_ingestion[group_name].deployment_binding.environment
        == inputs.environment
        &&
        module.source_ingestion[group_name].deployment_binding.source_endpoint.engine
        == inputs.source_endpoint.engine
        &&
        module.source_ingestion[group_name].deployment_binding.source_endpoint.database_name
        == inputs.source_endpoint.database_name
        &&
        module.source_ingestion[group_name].deployment_binding.target_endpoint.bucket_name
        == inputs.target.bucket_name
        &&
        module.source_ingestion[group_name].deployment_binding.target_endpoint.bucket_folder
        == inputs.target.bucket_folder
        &&
        jsonencode(
          module.source_ingestion[group_name].deployment_binding.replication_tasks[group_name].table_mappings
        )
        == jsonencode(
          jsondecode(inputs.replication_tasks[group_name].table_mappings)
        )
      )
    ])

    error_message = "Deployment bindings must retain the compiled identity, source, landing boundary and exact table mappings."
  }
}
