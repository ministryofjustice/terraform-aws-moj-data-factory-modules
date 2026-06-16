module "bucket" {
  source = "github.com/ministryofjustice/modernisation-platform-terraform-s3-bucket?ref=ce9c0c07489e393ce80441aed0fd5bf7798956a3"

  bucket_prefix      = "data-factory-${var.database_name}-"
  versioning_enabled = true
  ownership_controls = "BucketOwnerEnforced"

  replication_enabled = false
  providers = {
    aws.bucket-replication = aws
  }

  custom_kms_key = var.kms_key_arn

  tags = merge(
    var.tags,
    {
      "Name" = "data-factory-${var.database_name}-bucket"
    }
  )
}

resource "aws_iam_role" "lakeformation_s3_access_role" {
  name = "lakeformation-${var.database_name}-s3-access-role"

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
        Effect = "Allow"
        Action = ["s3:ListBucket"]
        Resource = [module.bucket.bucket.arn]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject"
        ]
        Resource = ["${module.bucket.bucket.arn}/*"]
      },
      {
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:Encrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = [var.kms_key_arn]
      }
    ]
  })
}

resource "aws_lakeformation_resource" "s3_location" {
  role_arn = aws_iam_role.lakeformation_s3_access_role.arn
  arn      = module.bucket.bucket.arn
}

resource "aws_glue_catalog_database" "data_factory_database" {
  name         = var.database_name
  location_uri = module.bucket.bucket.arn
  description  = "Glue catalog database for ${var.database_name}"
  tags = merge(
    var.tags,
    {
      "Name" = "data-factory-${var.database_name}-glue-database"
    }
  )
}
