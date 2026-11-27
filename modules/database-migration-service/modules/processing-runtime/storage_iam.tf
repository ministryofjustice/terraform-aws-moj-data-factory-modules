data "aws_partition" "current" {}

locals {
  processing_s3_arn = "arn:${data.aws_partition.current.partition}:s3:::"
  input_object_arns = distinct([
    for location in values(var.input_locations) :
    "${local.processing_s3_arn}${location.bucket_name}/${location.prefix}*"
  ])
  output_object_arns = [
    for directory in ["data", "results"] :
    "${local.processing_s3_arn}${var.output.bucket_name}/${var.output.prefix}${directory}/*"
  ]
}

data "aws_iam_policy_document" "worker_storage" {
  statement {
    actions   = ["s3:GetObjectVersion"]
    resources = local.input_object_arns
  }

  statement {
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
    ]
    resources = local.output_object_arns
  }

  dynamic "statement" {
    for_each = length(var.input_kms_key_arns) > 0 ? [var.input_kms_key_arns] : []

    content {
      actions   = ["kms:Decrypt"]
      resources = statement.value
    }
  }

  dynamic "statement" {
    for_each = var.output_kms_key_arn == null ? [] : [var.output_kms_key_arn]

    content {
      actions = [
        "kms:Decrypt",
        "kms:GenerateDataKey",
      ]
      resources = [statement.value]
    }
  }
}

resource "aws_iam_role_policy" "worker_storage" {
  name   = "processing-storage"
  role   = aws_iam_role.worker.name
  policy = data.aws_iam_policy_document.worker_storage.json
}
