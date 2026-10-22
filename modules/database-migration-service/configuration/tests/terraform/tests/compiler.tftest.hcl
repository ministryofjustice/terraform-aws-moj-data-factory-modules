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
}
