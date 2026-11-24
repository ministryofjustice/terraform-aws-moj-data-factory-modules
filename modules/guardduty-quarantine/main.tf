module "quarantine_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.8.0"

  function_name = var.lambda_function_name
  description   = "Routes GuardDuty malware scan failures to quarantine storage"

  package_type = var.package_type

  create_package         = false
  local_existing_package = var.package_type == "Zip" ? var.lambda_zip_path : null
  image_uri              = var.package_type == "Image" ? var.lambda_image_uri : null

  runtime = var.package_type == "Zip" ? var.runtime : null
  handler = var.package_type == "Zip" ? var.handler : null

  timeout     = var.timeout
  memory_size = var.memory_size

  environment_variables = {
    SOURCE_BUCKET     = var.source_bucket.bucket.s3_bucket_id
    QUARANTINE_BUCKET = var.quarantine_bucket.bucket.s3_bucket_id
    QUARANTINE_PREFIX = var.quarantine_prefix
    ACTION_ON_MALWARE = var.action_on_malware
  }

  create_role        = true
  attach_policy_json = true
  policy_json        = data.aws_iam_policy_document.quarantine_lambda.json

  cloudwatch_logs_retention_in_days = 30

  tags = local.common_tags
}

resource "aws_cloudwatch_event_rule" "guardduty_quarantine" {
  name        = var.eventbridge_rule_name
  description = "Invoke quarantine Lambda when GuardDuty reports an unsafe or failed S3 object scan."

  event_pattern = jsonencode({
    source      = ["aws.guardduty"]
    detail-type = ["GuardDuty Malware Protection Object Scan Result"]

    detail = {
      s3ObjectDetails = {
        bucketName = [var.source_bucket.bucket.s3_bucket_id]
      }
      scanResultDetails = {
        scanResultStatus = var.scan_result_statuses
      }
    }
  })

  tags = local.common_tags
}

resource "aws_cloudwatch_event_target" "guardduty_quarantine_lambda" {
  rule      = aws_cloudwatch_event_rule.guardduty_quarantine.name
  target_id = module.quarantine_lambda.lambda_function_name
  arn       = module.quarantine_lambda.lambda_function_arn
}

resource "aws_lambda_permission" "allow_eventbridge_quarantine" {
  statement_id  = "AllowExecutionFromEventBridgeGuardDutyQuarantine"
  action        = "lambda:InvokeFunction"
  function_name = module.quarantine_lambda.lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.guardduty_quarantine.arn
}

data "aws_iam_policy_document" "quarantine_lambda" {
  statement {
    sid    = "Logs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents"
    ]

    resources = [
      "arn:${data.aws_partition.current.partition}:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
    ]
  }

  statement {
    sid    = "ReadSourceObject"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectTagging",
      "s3:GetObjectVersion",
      "s3:GetObjectVersionTagging"
    ]

    resources = [
      "arn:${data.aws_partition.current.partition}:s3:::${var.source_bucket.bucket.s3_bucket_id}/*"
    ]
  }

  statement {
    sid    = "WriteQuarantineObject"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:PutObjectTagging",
      "s3:AbortMultipartUpload"
    ]

    resources = [
      "arn:${data.aws_partition.current.partition}:s3:::${var.quarantine_bucket.bucket.s3_bucket_id}/*"
    ]
  }

  statement {
    sid    = "ListBucketsForSanity"
    effect = "Allow"
    actions = [
      "s3:ListBucket"
    ]

    resources = [
      "arn:${data.aws_partition.current.partition}:s3:::${var.source_bucket.bucket.s3_bucket_id}",
      "arn:${data.aws_partition.current.partition}:s3:::${var.quarantine_bucket.bucket.s3_bucket_id}"
    ]
  }

  statement {
    sid    = "DecryptSourceBucketObjects"
    effect = "Allow"
    actions = [
      "kms:Decrypt"
    ]

    resources = [
      var.source_bucket.kms_key_arn
    ]
  }

  statement {
    sid    = "EncryptQuarantineBucketObjects"
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:GenerateDataKey"
    ]

    resources = [
      var.quarantine_bucket.kms_key_arn
    ]
  }
}
