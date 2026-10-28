moved {
  from = aws_dms_replication_instance.this
  to   = module.capacity[0].aws_dms_replication_instance.this
}

moved {
  from = aws_security_group.replication_instance
  to   = module.capacity[0].aws_security_group.replication_instance
}

moved {
  from = aws_vpc_security_group_egress_rule.replication_instance[0]
  to   = module.capacity[0].aws_vpc_security_group_egress_rule.replication_instance[0]
}

moved {
  from = aws_dms_replication_subnet_group.this[0]
  to   = module.capacity[0].aws_dms_replication_subnet_group.this[0]
}

moved {
  from = aws_cloudwatch_log_group.replication_tasks[0]
  to   = module.capacity[0].aws_cloudwatch_log_group.replication_tasks[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.replication_instance_cpu_utilization[0]
  to   = module.capacity[0].aws_cloudwatch_metric_alarm.replication_instance_cpu_utilization[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.replication_instance_free_storage_space[0]
  to   = module.capacity[0].aws_cloudwatch_metric_alarm.replication_instance_free_storage_space[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.replication_instance_freeable_memory[0]
  to   = module.capacity[0].aws_cloudwatch_metric_alarm.replication_instance_freeable_memory[0]
}
