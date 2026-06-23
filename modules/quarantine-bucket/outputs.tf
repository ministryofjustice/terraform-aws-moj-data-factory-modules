output "bucket_id" {
  description = "Name (ID) of the quarantine bucket"
  value       = module.quarantine_bucket.s3_bucket_id
}

output "bucket_arn" {
  description = "ARN of the quarantine bucket"
  value       = module.quarantine_bucket.s3_bucket_arn
}

output "bucket_domain_name" {
  description = "Domain name of the quarantine bucket"
  value       = module.quarantine_bucket.s3_bucket_bucket_domain_name
}

output "kms_key_arn" {
  description = "ARN of the KMS key used to encrypt the bucket (created or supplied)"
  value       = local.kms_key_arn
}

output "kms_key_alias" {
  description = "Alias of the KMS key created by the module"
  value       = aws_kms_alias.this.name
}

output "sns_topic_arn" {
  description = "ARN of the SNS topic that receives quarantine notifications"
  value       = aws_sns_topic.this.arn
}
