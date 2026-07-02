
data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  common_tags = merge(
    var.tags,
    {
      ManagedBy = "Terraform"
      Module    = "s3-raw-bucket"
    }
  )
# Constructs the ARN of the S3 bucket.
  bucket_arn        = "arn:aws:s3:::${var.bucket_name}"

# Constructs the ARN of all objects in the S3 bucket.
  bucket_objects_arn = "arn:aws:s3:::${var.bucket_name}/*"

}