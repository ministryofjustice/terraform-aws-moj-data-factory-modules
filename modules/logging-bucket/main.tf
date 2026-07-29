module "log_bucket" {
  source = "git::https://github.com/terraform-aws-modules/terraform-aws-s3-bucket.git?ref=0c0fb28347cc253088fe3966dca67420d39fbbe9"

  bucket           = "logs-${local.account_id}-${local.region}-an"
  bucket_namespace = "account-regional"
  force_destroy    = var.force_destroy

  control_object_ownership = true

  attach_access_log_delivery_policy     = true
  attach_deny_insecure_transport_policy = true
  attach_require_latest_tls_policy      = true

  server_side_encryption_configuration = {
    rule = {
      apply_server_side_encryption_by_default = {
        sse_algorithm = "AES256"
      }
    }
  }

  access_log_delivery_policy_source_accounts = [local.account_id]
  access_log_delivery_policy_source_buckets  = ["arn:aws:s3:::*-${local.account_id}-${local.region}-an"]

  tags = local.tags

  lifecycle_rule = [
    {
      id      = "access-log-retention"
      enabled = true

      transition = [
        {
          days          = 90
          storage_class = "STANDARD_IA"
        },
        {
          days          = 365
          storage_class = "GLACIER"
        }
      ]

      noncurrent_version_transition = [
        {
          days          = 45
          storage_class = "STANDARD_IA"
        },
        {
          days          = 180
          storage_class = "GLACIER"
        }
      ]

      noncurrent_version_expiration = {
        days = 365
      }
    }
  ]
}