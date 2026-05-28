data "aws_iam_policy_document" "trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "sts.windows.net/${var.tenant_id}/:aud"
      values   = ["https://analysis.windows.net/powerbi/connector/AmazonS3"]
    }

    condition {
      test     = "StringEquals"
      variable = "sts.windows.net/${var.tenant_id}/:sub"
      values   = [var.object_id]
    }
  }
}

resource "aws_iam_role" "this" {
  name               = var.role_name
  assume_role_policy = data.aws_iam_policy_document.trust.json
}

data "aws_iam_policy_document" "bucket_access" {
  statement {
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [var.bucket_arn]
  }

  statement {
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${var.bucket_arn}/*"]
  }
}

resource "aws_iam_role_policy" "this" {
  name   = var.role_policy_name
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.bucket_access.json
}
