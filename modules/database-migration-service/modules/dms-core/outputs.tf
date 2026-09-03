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

output "source_endpoint_arn" {
  description = "ARN of the DMS source endpoint."
  value       = aws_dms_endpoint.source.endpoint_arn
}

output "source_endpoint_id" {
  description = "Identifier of the DMS source endpoint."
  value       = aws_dms_endpoint.source.endpoint_id
}

output "target_endpoint_arn" {
  description = "ARN of the DMS S3 target endpoint."
  value       = aws_dms_s3_endpoint.target.endpoint_arn
}

output "target_endpoint_id" {
  description = "Identifier of the DMS S3 target endpoint."
  value       = aws_dms_s3_endpoint.target.endpoint_id
}

output "source_secrets_manager_access_role_arn" {
  description = "ARN of the IAM role used by AWS DMS to access the source secret."
  value       = local.source_secrets_manager_access_role_arn
}

output "s3_target_service_access_role_arn" {
  description = "ARN of the IAM role used by AWS DMS to access the S3 target."
  value       = local.s3_target_service_access_role_arn
}
