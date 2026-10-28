locals {
  task_logging_settings = {
    EnableLogging = var.task_logging.enabled
  }

  replication_task_settings = {
    for key, task in var.replication_tasks : key => jsonencode(merge(
      task.replication_task_settings == null ? {} : jsondecode(task.replication_task_settings),
      {
        Logging = local.task_logging_settings
      }
    ))
  }
}

resource "aws_dms_replication_task" "this" {
  for_each = var.replication_tasks

  replication_task_id = each.value.replication_task_id
  migration_type      = each.value.migration_type

  replication_instance_arn = local.replication_capacity.replication_instance_arn
  source_endpoint_arn      = aws_dms_endpoint.source.endpoint_arn
  target_endpoint_arn      = aws_dms_s3_endpoint.target.endpoint_arn

  table_mappings            = each.value.table_mappings
  replication_task_settings = local.replication_task_settings[each.key]

  start_replication_task = false

  tags = merge(
    var.tags,
    each.value.tags,
    {
      Name = each.value.replication_task_id
    }
  )

  lifecycle {
    ignore_changes = [
      start_replication_task
    ]
  }

  depends_on = [
    module.capacity
  ]
}
