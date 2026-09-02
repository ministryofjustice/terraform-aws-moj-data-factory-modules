output "replication_instance_arn" {
  description = "ARN of the DMS replication instance."
  value       = aws_dms_replication_instance.this.replication_instance_arn
}

output "replication_instance_id" {
  description = "Identifier of the DMS replication instance."
  value       = aws_dms_replication_instance.this.replication_instance_id
}

output "replication_security_group_id" {
  description = "ID of the security group created by this module for the DMS replication instance."
  value       = aws_security_group.replication_instance.id
}

output "replication_security_group_ids" {
  description = "Security group IDs attached to the DMS replication instance."
  value       = local.replication_security_group_ids
}

output "replication_subnet_group_id" {
  description = "ID of the DMS replication subnet group used by the replication instance."
  value       = local.replication_subnet_group_id
}
