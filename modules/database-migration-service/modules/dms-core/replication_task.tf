resource "aws_dms_replication_task" "this" {
  for_each = var.replication_tasks

  replication_task_id = each.value.replication_task_id
  migration_type      = each.value.migration_type

  replication_instance_arn = aws_dms_replication_instance.this.replication_instance_arn
  source_endpoint_arn      = aws_dms_endpoint.source.endpoint_arn
  target_endpoint_arn      = aws_dms_s3_endpoint.target.endpoint_arn

  table_mappings            = each.value.table_mappings
  replication_task_settings = each.value.replication_task_settings

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
}