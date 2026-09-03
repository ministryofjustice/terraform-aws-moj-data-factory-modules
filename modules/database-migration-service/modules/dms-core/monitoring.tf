resource "aws_cloudwatch_metric_alarm" "replication_instance_cpu_utilization" {
  count = var.monitoring.enabled ? 1 : 0

  alarm_name        = "${var.name}-dms-cpu-utilization"
  alarm_description = "DMS replication instance CPU utilization is above the configured threshold."

  namespace   = "AWS/DMS"
  metric_name = "CPUUtilization"
  statistic   = "Average"

  period             = var.monitoring.period_seconds
  evaluation_periods = var.monitoring.evaluation_periods

  threshold           = var.monitoring.cpu_utilization_threshold
  comparison_operator = "GreaterThanThreshold"

  dimensions = {
    ReplicationInstanceIdentifier = aws_dms_replication_instance.this.replication_instance_id
  }

  treat_missing_data = "notBreaching"

  alarm_actions             = var.monitoring.alarm_action_arns
  ok_actions                = var.monitoring.ok_action_arns
  insufficient_data_actions = var.monitoring.insufficient_data_action_arns

  tags = merge(
    var.tags,
    {
      Name = "${var.name}-dms-cpu-utilization"
    }
  )
}

resource "aws_cloudwatch_metric_alarm" "replication_instance_free_storage_space" {
  count = var.monitoring.enabled ? 1 : 0

  alarm_name        = "${var.name}-dms-free-storage-space"
  alarm_description = "DMS replication instance free storage space is below the configured threshold."

  namespace   = "AWS/DMS"
  metric_name = "FreeStorageSpace"
  statistic   = "Minimum"

  period             = var.monitoring.period_seconds
  evaluation_periods = var.monitoring.evaluation_periods

  threshold           = var.monitoring.free_storage_space_threshold_bytes
  comparison_operator = "LessThanThreshold"

  dimensions = {
    ReplicationInstanceIdentifier = aws_dms_replication_instance.this.replication_instance_id
  }

  treat_missing_data = "notBreaching"

  alarm_actions             = var.monitoring.alarm_action_arns
  ok_actions                = var.monitoring.ok_action_arns
  insufficient_data_actions = var.monitoring.insufficient_data_action_arns

  tags = merge(
    var.tags,
    {
      Name = "${var.name}-dms-free-storage-space"
    }
  )
}

resource "aws_cloudwatch_metric_alarm" "replication_instance_freeable_memory" {
  count = var.monitoring.enabled ? 1 : 0

  alarm_name        = "${var.name}-dms-freeable-memory"
  alarm_description = "DMS replication instance freeable memory is below the configured threshold."

  namespace   = "AWS/DMS"
  metric_name = "FreeableMemory"
  statistic   = "Minimum"

  period             = var.monitoring.period_seconds
  evaluation_periods = var.monitoring.evaluation_periods

  threshold           = var.monitoring.freeable_memory_threshold_bytes
  comparison_operator = "LessThanThreshold"

  dimensions = {
    ReplicationInstanceIdentifier = aws_dms_replication_instance.this.replication_instance_id
  }

  treat_missing_data = "notBreaching"

  alarm_actions             = var.monitoring.alarm_action_arns
  ok_actions                = var.monitoring.ok_action_arns
  insufficient_data_actions = var.monitoring.insufficient_data_action_arns

  tags = merge(
    var.tags,
    {
      Name = "${var.name}-dms-freeable-memory"
    }
  )
}