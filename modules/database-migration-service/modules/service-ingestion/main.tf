module "source_ingestion" {
  for_each = var.compiled_artifact.module_inputs

  source = "../dms-source-ingestion"

  existing_replication_capacity = lookup(
    var.replication_capacities,
    each.key,
    null
  )

  name        = each.value.name
  environment = each.value.environment
  vpc_id      = each.value.vpc_id

  network              = each.value.network
  replication_instance = each.value.replication_instance
  source_endpoint      = each.value.source_endpoint
  replication_tasks    = each.value.replication_tasks

  target = merge(each.value.target, {
    cdc = merge(try(each.value.target.cdc, {}), {
      use_table_paths = true
    })
  })

  tags = var.tags
}
