variable "contract_runtimes" {
  description = "Contract runtime configuration and execution permissions keyed by source and extraction generation."

  type = map(object({
    configuration_json = string
    execution_policy   = string
  }))

  default  = {}
  nullable = false
}

data "aws_iam_policy_document" "contract_runtime_trust" {
  count = length(var.contract_runtimes) > 0 ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

locals {
  contract_runtime_names = {
    for key in keys(var.contract_runtimes) :
    key => "${substr(var.name_prefix, 0, 35)}-contract-${substr(sha256(key), 0, 16)}"
  }
}

resource "aws_cloudwatch_log_group" "contract_runtime" {
  for_each = var.contract_runtimes

  name              = "/aws/lambda/${local.contract_runtime_names[each.key]}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_iam_role" "contract_runtime" {
  for_each = var.contract_runtimes

  name               = local.contract_runtime_names[each.key]
  assume_role_policy = data.aws_iam_policy_document.contract_runtime_trust[0].json
  tags               = var.tags
}

data "aws_iam_policy_document" "contract_runtime_execution" {
  for_each = var.contract_runtimes

  source_policy_documents = [each.value.execution_policy]

  statement {
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]

    resources = ["${aws_cloudwatch_log_group.contract_runtime[each.key].arn}:*"]
  }
}

resource "aws_iam_role_policy" "contract_runtime" {
  for_each = var.contract_runtimes

  name   = "contract-runtime"
  role   = aws_iam_role.contract_runtime[each.key].name
  policy = data.aws_iam_policy_document.contract_runtime_execution[each.key].json
}

resource "aws_lambda_function" "contract_runtime" {
  for_each = var.contract_runtimes

  function_name = local.contract_runtime_names[each.key]
  role          = aws_iam_role.contract_runtime[each.key].arn

  package_type  = "Zip"
  handler       = "dms_runtime.contract_lambda_function.lambda_handler"
  runtime       = "python3.12"
  architectures = ["x86_64"]
  publish       = true

  s3_bucket         = var.artifact.bucket_name
  s3_key            = var.artifact.object_key
  s3_object_version = var.artifact.version_id
  source_code_hash  = var.artifact.source_code_hash

  timeout     = 900
  memory_size = 256
  tags        = var.tags

  environment {
    variables = {
      CONTRACT_RUNTIME_CONFIG = each.value.configuration_json
    }
  }

  depends_on = [aws_iam_role_policy.contract_runtime]

  lifecycle {
    precondition {
      condition = (
        contains(keys(var.compiled_artifacts), each.key) &&
        contains(keys(var.contract_catalogs), each.key)
      )
      error_message = "Each contract runtime must have a compiled deployment and a prepared catalog."
    }
  }
}

output "contract_runtime_function_arns" {
  description = "Published contract runtime Lambda ARNs keyed by source and extraction generation."
  value = {
    for key, function in aws_lambda_function.contract_runtime :
    key => function.qualified_arn
  }
}