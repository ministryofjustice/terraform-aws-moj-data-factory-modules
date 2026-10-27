data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

data "aws_partition" "current" {}

locals {
  dms_arn_prefix = (
    "arn:${data.aws_partition.current.partition}:dms:${var.service_context.region}:${var.service_context.account_id}:"
  )

  lambda_arn_prefix = (
    "arn:${data.aws_partition.current.partition}:lambda:${var.service_context.region}:${var.service_context.account_id}:function:"
  )

  registry_arn = (
    "arn:${data.aws_partition.current.partition}:dynamodb:${var.service_context.region}:${var.service_context.account_id}:table/${var.registry.table_name}"
  )
}

resource "aws_cloudwatch_log_group" "runtime" {
  name              = "/aws/lambda/${var.function_name}"
  retention_in_days = var.log_retention_days

  tags = var.tags
}

resource "aws_lambda_function" "runtime" {
  function_name = var.function_name
  role          = aws_iam_role.runtime.arn

  package_type = "Zip"
  handler      = "dms_runtime.lambda_function.lambda_handler"
  runtime      = "python3.12"
  architectures = [
    "x86_64",
  ]

  s3_bucket         = var.artifact.bucket_name
  s3_key            = var.artifact.object_key
  s3_object_version = var.artifact.version_id
  source_code_hash  = var.artifact.source_code_hash

  timeout     = 900
  memory_size = 256

  environment {
    variables = merge(
      {
        RUNTIME_ENVIRONMENT           = var.service_context.environment
        RUNTIME_ACCOUNT_ID            = var.service_context.account_id
        RUNTIME_SOURCE                = var.service_context.source
        RUNTIME_DATABASE_NAME         = var.service_context.database_name
        INGESTION_REGISTRY_TABLE_NAME = var.registry.table_name
        PUBLISHED_DOCUMENT_BUCKET     = var.published_documents.bucket_name
        PUBLISHED_DOCUMENT_PREFIX     = var.published_documents.prefix
        PUBLISHED_DOCUMENT_BUCKET_OWNER = (
          var.published_documents.bucket_owner
        )
        SOURCE_CREDENTIAL_MODE = var.source_credentials.mode
      },
      var.source_credentials.mode == "synchronise_then_test" ? {
        CREDENTIAL_SYNC_FUNCTION_ARN = var.source_credentials.sync_function_arn
      } : {}
    )
  }

  depends_on = [
    aws_cloudwatch_log_group.runtime,
    aws_iam_role_policy.runtime,
  ]

  lifecycle {
    precondition {
      condition = (
        data.aws_caller_identity.current.account_id
        == var.service_context.account_id
      )

      error_message = "service_context.account_id must match the AWS provider account."
    }

    precondition {
      condition = (
        data.aws_region.current.region
        == var.service_context.region
      )

      error_message = "service_context.region must match the AWS provider region."
    }

    precondition {
      condition     = var.registry.table_arn == local.registry_arn
      error_message = "registry.table_arn must identify the named table in the authorised account and region."
    }

    precondition {
      condition = alltrue([
        for arn in var.dms_resources.replication_task_arns :
        startswith(arn, "${local.dms_arn_prefix}task:") &&
        !strcontains(arn, "*") &&
        !strcontains(arn, "?")
      ])

      error_message = "Replication task ARNs must identify tasks in the authorised account and region without wildcards."
    }

    precondition {
      condition = alltrue([
        for arn in var.dms_resources.replication_instance_arns :
        startswith(arn, "${local.dms_arn_prefix}rep:") &&
        !strcontains(arn, "*") &&
        !strcontains(arn, "?")
      ])

      error_message = "Replication instance ARNs must identify instances in the authorised account and region without wildcards."
    }

    precondition {
      condition = alltrue([
        for arn in var.dms_resources.source_endpoint_arns :
        startswith(arn, "${local.dms_arn_prefix}endpoint:") &&
        !strcontains(arn, "*") &&
        !strcontains(arn, "?")
      ])

      error_message = "Source endpoint ARNs must identify endpoints in the authorised account and region without wildcards."
    }

    precondition {
      condition = (
        var.source_credentials.mode == "connection_test"
        ? true
        : try(
          startswith(
            var.source_credentials.sync_function_arn,
            local.lambda_arn_prefix
          ) &&
          !strcontains(var.source_credentials.sync_function_arn, "*") &&
          !strcontains(var.source_credentials.sync_function_arn, "?"),
          false
        )
      )

      error_message = "The credential-sync Lambda ARN must belong to the authorised account and region without wildcards."
    }

    precondition {
      condition = (
        !strcontains(var.published_documents.prefix, "*") &&
        !strcontains(var.published_documents.prefix, "?")
      )

      error_message = "The published document prefix must not contain IAM wildcard characters."
    }
  }

  tags = var.tags
}
