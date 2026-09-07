data "template_file" "table-mappings" {
  template = var.table_mappings

  vars = {
    input_schema = var.rename_rule_source_schema
    output_space = var.rename_rule_output_space
  }
}

resource "aws_dms_replication_task" "dms_replication" {
  count = var.enable_replication_task ? 1 : 0

  replication_task_id      = "${var.name}-task-${var.env}"
  migration_type           = var.migration_type
  replication_instance_arn = var.dms_replication_instance
  source_endpoint_arn      = var.dms_source_endpoint
  target_endpoint_arn      = var.dms_target_endpoint

  table_mappings            = replace(data.template_file.table-mappings.rendered, "\\s", "")
  replication_task_settings = var.replication_task_settings

  cdc_start_time     = var.cdc_start_time
  cdc_start_position = var.cdc_start_position

  start_replication_task = false

  tags = merge(
    var.tags,
    {
      name = "${var.name}-task-${var.env}"
    }
  )

  lifecycle {
    ignore_changes = [
      replication_task_settings,
      cdc_start_position,
      cdc_start_time
    ]
  }
}
