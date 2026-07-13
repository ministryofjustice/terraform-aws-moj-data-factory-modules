
# Returns the name (ID) of the S3 bucket created by this module.
output "bucket_name" {
  description = "Name of the scanned S3 bucket."
  value       = module.bucket.s3_bucket_id
}

# Returns the Amazon Resource Name (ARN) of the bucket.
output "bucket_arn" {
  description = "ARN of the scanned S3 bucket."
  value       = module.bucket.s3_bucket_arn
}

