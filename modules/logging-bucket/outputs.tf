output "log_bucket_name" {
  description = "The name of the central log bucket."
  value       = module.log_bucket.s3_bucket_id
}