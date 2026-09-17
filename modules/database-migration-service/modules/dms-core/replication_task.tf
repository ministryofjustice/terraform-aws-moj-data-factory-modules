locals {
  task_logging_settings = merge(
    {
      EnableLogging  = var.task_logging.enabled
      DeleteTaskLogs = false
    },
    var.task_logging.enabled ? {
      LogComponents = [
        for component in sort(keys(var.task_logging.log_components)) : {
          Id       = component
          Severity = var.task_logging.log_components[component]
        }
      ]
    } : {}
  )

  replication_task_settings = {
    for key, task in var.replication_tasks : key => jsonencode(merge(
      task.replication_task_settings == null ? {} : jsondecode(task.replication_task_settings),
      {
        Logging = local.task_logging_settings
      }
    ))
  }
}

resource "aws_cloudwatch_log_group" "replication_tasks" {
  count = var.task_logging.enabled && length(var.replication_tasks) > 0 ? 1 : 0

  name              = "dms-tasks-${var.replication_instance.replication_instance_id}"
  retention_in_days = var.task_logging.retention_in_days
  kms_key_id        = var.task_logging.kms_key_arn

  tags = merge(
    {
      Name = "dms-tasks-${var.replication_instance.replication_instance_id}"
    },
    var.tags
  )
}

resource "aws_dms_replication_task" "this" {
  for_each = var.replication_tasks

  replication_task_id = each.value.replication_task_id
  migration_type      = each.value.migration_type

  replication_instance_arn = aws_dms_replication_instance.this.replication_instance_arn
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
    aws_cloudwatch_log_group.replication_tasks
  ]
}
