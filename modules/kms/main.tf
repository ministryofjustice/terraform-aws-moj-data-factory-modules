data "aws_caller_identity" "current" {}

resource "aws_kms_key" "this" {
    description             = var.description 
    enable_key_rotation     = var.enable_key_rotation 
    deletion_window_in_days = var.deletion_window_in_days
    policy                  = data.aws_iam_policy_document.key_policy.json
}

