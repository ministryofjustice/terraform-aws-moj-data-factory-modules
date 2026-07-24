# Returns the name (ID) of the S3 bucket created by this module.
output "bucket" {
  description = "The S3 bucket created by this module."
  value       = module.bucket
}

output "guardduty_scan_role_name" {
  description = "Name of the IAM role used by GuardDuty Malware Protection, if enabled."
  value       = try(module.guardduty_scan_role[0].name, null)
}

output "guardduty_scan_role_arn" {
  description = "ARN of the IAM role used by GuardDuty Malware Protection, if enabled."
  value       = try(module.guardduty_scan_role[0].arn, null)
}

output "guardduty_malware_protection_plan_id" {
  description = "ID of the GuardDuty Malware Protection plan, if enabled."
  value       = try(aws_guardduty_malware_protection_plan.malware_protection_plan[0].id, null)
}

output "kms_key_arn" {
  description = "KMS key ARN used for bucket encryption."
  value       = var.kms_key_arn
}

