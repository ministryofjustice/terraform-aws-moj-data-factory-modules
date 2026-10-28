output "replication_instance_arn" {
  description = "ARN of the DMS replication instance."
  value       = local.replication_capacity.replication_instance_arn
}

output "replication_instance_id" {
  description = "Identifier of the DMS replication instance."
  value       = local.replication_capacity.replication_instance_id
}

output "replication_security_group_id" {
  description = "ID of the security group created by this module for the DMS replication instance."
  value       = local.replication_capacity.replication_security_group_id
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

output "replication_task_log_group" {
  description = "Task log group owned by the selected replication capacity."
  value       = local.replication_capacity.task_log_group
}

output "replication_tasks" {
  description = "DMS replication task identifiers exposed for downstream orchestration and monitoring."

  value = {
    for key, task in aws_dms_replication_task.this : key => {
      arn            = task.replication_task_arn
      id             = task.replication_task_id
      migration_type = task.migration_type
    }
  }
}

output "resource_binding" {
  description = "DMS resource references and configured extraction boundaries for deployment manifest generation."

  value = {
    replication_instance_arn = local.replication_capacity.replication_instance_arn

    source_endpoint = {
      arn           = aws_dms_endpoint.source.endpoint_arn
      engine        = aws_dms_endpoint.source.engine_name
      database_name = aws_dms_endpoint.source.database_name
    }

    target_endpoint = {
      arn           = aws_dms_s3_endpoint.target.endpoint_arn
      bucket_name   = aws_dms_s3_endpoint.target.bucket_name
      bucket_folder = aws_dms_s3_endpoint.target.bucket_folder
      cdc_path      = aws_dms_s3_endpoint.target.cdc_path
    }

    replication_tasks = {
      for key, task in aws_dms_replication_task.this : key => {
        arn                      = task.replication_task_arn
        id                       = task.replication_task_id
        migration_type           = task.migration_type
        replication_instance_arn = task.replication_instance_arn
        source_endpoint_arn      = task.source_endpoint_arn
        target_endpoint_arn      = task.target_endpoint_arn
        table_mappings           = jsondecode(task.table_mappings)
      }
    }
  }
}

output "replication_capacity" {
  description = "Selected replication capacity and its shared resource references."
  value       = local.replication_capacity
}
