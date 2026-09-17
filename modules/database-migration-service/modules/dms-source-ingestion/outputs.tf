output "dms_replication_instance_arn" {
  description = "ARN of the DMS replication instance."
  value       = module.dms_core.replication_instance_arn
}

output "dms_source_endpoint_arn" {
  description = "ARN of the DMS source endpoint."
  value       = module.dms_core.source_endpoint_arn
}

output "dms_target_endpoint_arn" {
  description = "ARN of the DMS S3 target endpoint."
  value       = module.dms_core.target_endpoint_arn
}

output "replication_task_log_group" {
  description = "CloudWatch log group managed for DMS replication tasks or null when task logging is disabled or no tasks are configured."
  value       = module.dms_core.replication_task_log_group
}

output "replication_tasks" {
  description = "DMS replication tasks keyed by task name, including ARN, ID and migration type."
  value       = module.dms_core.replication_tasks
}
