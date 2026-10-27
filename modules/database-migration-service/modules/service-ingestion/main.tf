module "source_ingestion" {
  for_each = var.compiled_artifact.module_inputs

  source = "../dms-source-ingestion"

  name        = each.value.name
  environment = each.value.environment
  vpc_id      = each.value.vpc_id

  network              = each.value.network
  replication_instance = each.value.replication_instance
  source_endpoint      = each.value.source_endpoint
  target               = each.value.target
  replication_tasks    = each.value.replication_tasks

  tags = var.tags
}
