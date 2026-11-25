locals {
  raw_history_services = {
    for source, settings in var.raw_history_runtimes :
    source => settings
    if contains(local.services, source)
  }

  raw_history_yaml_tables = {
    for source in keys(local.raw_history_services) :
    source => toset(flatten([
      for artifact in values(var.compiled_artifacts) : [
        for group in values(artifact.runtime_configuration.task_groups) : [
          for table in group.tables :
          jsonencode([
            table.schema_name,
            table.table_name,
            table.history_profile,
          ])
        ]
      ]
      if artifact.runtime_configuration.source == source
    ]))
  }

  raw_history_runtime_outputs = {
    for source, runtime in module.raw_history :
    source => {
      function_name = "${var.name_prefix}-${source}-raw-history"

      function_arn           = runtime.function_arn
      qualified_function_arn = runtime.qualified_function_arn
      function_version       = runtime.function_version
      execution_role_arn     = runtime.execution_role_arn

      operations_table        = runtime.operations_table
      configuration_reference = runtime.configuration_reference

      component_targets = {
        for profile in toset([
          for table in var.raw_history_runtimes[source].tables :
          table.profile
        ]) :
        profile => {
          kind = "lambda"
          arn  = runtime.qualified_function_arn
        }
      }
    }
  }
}

resource "terraform_data" "raw_history_contract" {
  for_each = local.raw_history_services

  input = each.key

  lifecycle {
    precondition {
      condition = alltrue([
        for table in each.value.tables :
        contains(
          local.raw_history_yaml_tables[each.key],
          jsonencode([
            table.schema_name,
            table.table_name,
            table.profile,
          ])
        )
      ])

      error_message = "Each Raw History table and profile must match a table declared in the service's compiled YAML configuration."
    }
  }
}

module "raw_history" {
  for_each = local.raw_history_services

  source = "../raw-history-runtime"

  function_name = "${var.name_prefix}-${each.key}-raw-history"

  service_context = {
    environment = local.service_artifacts[
      each.key
    ].runtime_configuration.environment

    region = local.service_artifacts[
      each.key
    ].runtime_configuration.region

    account_id = data.aws_caller_identity.current.account_id
    source     = each.key

    database_name = local.service_artifacts[
      each.key
    ].runtime_configuration.source_endpoint.database_name
  }

  artifact             = var.artifact
  registry             = var.registry
  registry_kms_key_arn = var.registry_kms_key_arn

  published_documents = var.runtime_settings[
    each.key
  ].published_documents

  configuration_store = each.value.configuration_store
  history_bucket      = each.value.history_bucket

  operations_kms_key_arn      = each.value.operations_kms_key_arn
  max_batch_bytes             = each.value.max_batch_bytes
  deletion_protection_enabled = each.value.deletion_protection_enabled

  tables = each.value.tables

  log_retention_days = var.log_retention_days
  tags               = var.tags

  depends_on = [
    terraform_data.raw_history_contract,
  ]
}
