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
      variable = "${var.oidc_provider_condition_key_prefix}aud"
      values   = [var.audience]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider_condition_key_prefix}sub"
      values   = [var.object_id]
    }
  }
}

resource "aws_iam_role" "this" {
  name               = var.role_name
  assume_role_policy = data.aws_iam_policy_document.trust.json

  tags = merge(
    { Name = var.role_name },
    var.additional_tags,
  )
}

data "aws_iam_policy_document" "bucket_access" {
  statement {
    effect    = "Allow"
    actions   = ["s3:*", "s3-object-lambda:*"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "this" {
  name   = var.role_policy_name
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.bucket_access.json
}
