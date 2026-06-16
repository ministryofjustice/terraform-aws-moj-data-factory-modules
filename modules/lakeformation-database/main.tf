/**
  * # Lake Formation Database
  * This module creates an S3 location registered in AWS Lake Formation and an AWS Glue Catalog Database backed by that S3 bucket.
  *
  * ## Usage
  * ```hcl
  * module "lakeformation_database" {
  *   source = "github.com/ministryofjustice/terraform-aws-moj-data-factory-modules//modules/lakeformation-database?ref=<git-sha>"
  *
  *   database_name = "example"
  *
  *   storage = {
  *     bucket_name = <bucket_name>
  *     prefix      = "example"
  *     kms_key_arn = "arn:aws:kms:eu-west-2:1234567890:key/example"
  *   }
  *   tags          = local.tags
  * }
  * ```
*/

resource "aws_iam_role" "lakeformation_s3_access_role" {
  name = "${var.database_name}-s3-access"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "lakeformation.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy" "lakeformation_s3_access_policy" {
  name = "lakeformation-s3-access-policy"
  role = aws_iam_role.lakeformation_s3_access_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = ["arn:aws:s3:::${var.storage.bucket_name}"]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject"
        ]
        Resource = ["arn:aws:s3:::${var.storage.bucket_name}/${var.storage.prefix}/*"]
      },
      {
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:Encrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = [var.storage.kms_key_arn]
      }
    ]
  })
}

resource "aws_lakeformation_resource" "s3_location" {
  role_arn = aws_iam_role.lakeformation_s3_access_role.arn
  arn      = "arn:aws:s3:::${var.storage.bucket_name}/${var.storage.prefix}"
}

resource "aws_glue_catalog_database" "data_factory_database" {
  name         = var.database_name
  location_uri = "s3://${var.storage.bucket_name}/${var.storage.prefix}"
  description  = "Glue catalog database for ${var.database_name}"
  tags = merge(
    var.tags,
    {
      "Name" = "data-factory-${var.database_name}-glue-database"
    }
  )
}
