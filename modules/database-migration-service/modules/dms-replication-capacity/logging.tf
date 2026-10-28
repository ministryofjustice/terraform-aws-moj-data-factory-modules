resource "aws_cloudwatch_log_group" "replication_tasks" {
  count = var.task_logging.enabled && var.create_task_log_group ? 1 : 0

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
