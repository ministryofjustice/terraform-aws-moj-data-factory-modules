variable "raw_batch_handoff" {
  description = "Optional Raw batch manifest destination and authorised landing locations."

  type = object({
    manifest = object({
      bucket_name = string
      prefix      = string
      kms_key_arn = optional(string)
    })
    landings = set(object({
      bucket_name  = string
      prefix       = string
      kms_key_arns = optional(set(string), [])
    }))
  })

  default = null

  validation {
    condition = var.raw_batch_handoff == null ? true : try(
      length(var.raw_batch_handoff.landings) > 0 && alltrue([
        for location in concat(
          [var.raw_batch_handoff.manifest],
          tolist(var.raw_batch_handoff.landings)
        ) :
        can(regex(
          "^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
          location.bucket_name
        )) &&
        endswith(location.prefix, "/") &&
        !strcontains(location.prefix, "*") &&
        !strcontains(location.prefix, "?") &&
        !strcontains(location.prefix, "\\") &&
        alltrue([
          for segment in split(
            "/",
            trimsuffix(location.prefix, "/")
          ) :
          !contains(["", ".", ".."], segment) &&
          segment == trimspace(segment)
        ])
      ]),
      false
    )

    error_message = "Raw batch locations require explicit bucket names and prefixes ending in '/', without wildcards or relative path segments."
  }

  validation {
    condition = var.raw_batch_handoff == null ? true : try(
      alltrue([
        for landing in var.raw_batch_handoff.landings :
        landing.bucket_name != var.raw_batch_handoff.manifest.bucket_name || (
          !startswith(
            landing.prefix,
            var.raw_batch_handoff.manifest.prefix
          ) &&
          !startswith(
            var.raw_batch_handoff.manifest.prefix,
            landing.prefix
          )
        )
      ]),
      false
    )

    error_message = "Raw landing and manifest prefixes must not overlap."
  }

  validation {
    condition = var.raw_batch_handoff == null ? true : try(
      alltrue([
        for arn in concat(
          var.raw_batch_handoff.manifest.kms_key_arn == null
          ? []
          : [var.raw_batch_handoff.manifest.kms_key_arn],
          flatten([
            for landing in var.raw_batch_handoff.landings :
            tolist(landing.kms_key_arns)
          ])
        ) :
        can(regex(
          "^arn:[a-z0-9-]+:kms:[a-z0-9-]+:[0-9]{12}:key/[A-Za-z0-9-]+$",
          arn
        ))
      ]),
      false
    )

    error_message = "Raw batch KMS permissions require exact key ARNs."
  }
}

locals {
  raw_batch_config = var.raw_batch_handoff == null ? {} : {
    RAW_BATCH_HANDOFF_CONFIG = jsonencode({
      manifest_location = {
        bucket_name           = var.raw_batch_handoff.manifest.bucket_name
        prefix                = var.raw_batch_handoff.manifest.prefix
        expected_bucket_owner = var.service_context.account_id
      }
      allowed_landings = [
        for landing in var.raw_batch_handoff.landings : {
          bucket_name = landing.bucket_name
          prefix      = landing.prefix
        }
      ]
    })
  }
}

data "aws_iam_policy_document" "raw_batch" {
  count = var.raw_batch_handoff == null ? 0 : 1

  dynamic "statement" {
    for_each = var.raw_batch_handoff.landings

    content {
      effect    = "Allow"
      actions   = ["s3:ListBucket"]
      resources = ["arn:${data.aws_partition.current.partition}:s3:::${statement.value.bucket_name}"]

      condition {
        test     = "StringLike"
        variable = "s3:prefix"
        values   = ["${statement.value.prefix}*"]
      }
    }
  }

  dynamic "statement" {
    for_each = var.raw_batch_handoff.landings

    content {
      effect    = "Allow"
      actions   = ["s3:GetObject", "s3:GetObjectVersion"]
      resources = ["arn:${data.aws_partition.current.partition}:s3:::${statement.value.bucket_name}/${statement.value.prefix}*"]
    }
  }

  statement {
    sid     = "ReadAndWriteRawBatchManifests"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]

    resources = [
      "arn:${data.aws_partition.current.partition}:s3:::${var.raw_batch_handoff.manifest.bucket_name}/${var.raw_batch_handoff.manifest.prefix}*",
    ]
  }

  statement {
    sid       = "LocateRawBatchManifests"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:${data.aws_partition.current.partition}:s3:::${var.raw_batch_handoff.manifest.bucket_name}"]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${var.raw_batch_handoff.manifest.prefix}*"]
    }
  }

  dynamic "statement" {
    for_each = length(flatten([
      for landing in var.raw_batch_handoff.landings :
      tolist(landing.kms_key_arns)
    ])) == 0 ? [] : [1]

    content {
      sid     = "DecryptRawBatchInputs"
      effect  = "Allow"
      actions = ["kms:Decrypt"]

      resources = sort(distinct(flatten([
        for landing in var.raw_batch_handoff.landings :
        tolist(landing.kms_key_arns)
      ])))
    }
  }

  dynamic "statement" {
    for_each = (
      var.raw_batch_handoff.manifest.kms_key_arn == null
      ? []
      : [var.raw_batch_handoff.manifest.kms_key_arn]
    )

    content {
      sid       = "EncryptAndReadRawBatchManifests"
      effect    = "Allow"
      actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
      resources = [statement.value]
    }
  }
}