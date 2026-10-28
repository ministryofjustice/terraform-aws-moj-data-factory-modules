data "aws_caller_identity" "current" {}

locals {
  services = toset([
    for artifact in values(var.compiled_artifacts) :
    artifact.runtime_configuration.source
  ])

  service_artifacts = {
    for name in local.services : name => [
      for key in sort(keys(var.compiled_artifacts)) :
      var.compiled_artifacts[key]
      if var.compiled_artifacts[key].runtime_configuration.source == name
    ][0]
  }

  generation_records = {
    for key, artifact in var.compiled_artifacts : key => {
      artifact_revision = artifact.artifact_revision

      capacity_arns = {
        for group_name, pool_name in var.capacity_assignments[key] :
        group_name => var.replication_capacities[pool_name].replication_instance_arn
      }
    }
  }
}

resource "terraform_data" "generation" {
  for_each = local.generation_records

  input            = each.value
  triggers_replace = [jsonencode(each.value)]

  lifecycle {
    prevent_destroy = true
  }
}

module "service" {
  for_each = var.compiled_artifacts

  source = "../service-ingestion"

  compiled_artifact = each.value

  replication_capacities = {
    for group_name, pool_name in var.capacity_assignments[each.key] :
    group_name => var.replication_capacities[pool_name]
  }

  tags = var.tags

  depends_on = [
    terraform_data.generation
  ]
}

module "runtime" {
  for_each = local.service_artifacts

  source = "../dms-runtime-controller"

  function_name = "${var.name_prefix}-${each.key}-runtime"

  service_context = {
    environment   = each.value.runtime_configuration.environment
    region        = each.value.runtime_configuration.region
    account_id    = data.aws_caller_identity.current.account_id
    source        = each.key
    database_name = each.value.runtime_configuration.source_endpoint.database_name
  }

  artifact            = var.artifact
  registry            = var.registry
  published_documents = var.runtime_settings[each.key].published_documents
  source_credentials  = var.runtime_settings[each.key].source_credentials

  dms_resources = {
    replication_instance_arns = distinct(flatten([
      for key, deployment in module.service : [
        for binding in values(deployment.deployment_bindings) :
        binding.replication_instance_arn
      ]
      if var.compiled_artifacts[key].runtime_configuration.source == each.key
    ]))

    source_endpoint_arns = distinct(flatten([
      for key, deployment in module.service : [
        for binding in values(deployment.deployment_bindings) :
        binding.source_endpoint.arn
      ]
      if var.compiled_artifacts[key].runtime_configuration.source == each.key
    ]))

    replication_task_arns = distinct(flatten([
      for key, deployment in module.service : flatten([
        for binding in values(deployment.deployment_bindings) : [
          for task in values(binding.replication_tasks) :
          task.arn
        ]
      ])
      if var.compiled_artifacts[key].runtime_configuration.source == each.key
    ]))
  }

  log_retention_days = var.log_retention_days
  tags               = var.tags
}
