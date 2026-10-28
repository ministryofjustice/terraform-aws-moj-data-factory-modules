output "binding" {
  description = "Platform-owned replication capacity and its shared operational resources."

  value = {
    vpc_id = var.vpc_id

    replication_instance_arn = aws_dms_replication_instance.this.replication_instance_arn
    replication_instance_id  = aws_dms_replication_instance.this.replication_instance_id

    replication_security_group_id  = aws_security_group.replication_instance.id
    replication_security_group_ids = local.replication_security_group_ids

    replication_subnet_group_id = local.replication_subnet_group_id

    task_log_group = length(aws_cloudwatch_log_group.replication_tasks) > 0 ? {
      arn  = aws_cloudwatch_log_group.replication_tasks[0].arn
      name = aws_cloudwatch_log_group.replication_tasks[0].name
    } : null
  }
}
