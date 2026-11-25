data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

data "aws_partition" "current" {}

locals {
  operations_table_name = "${var.function_name}-operations"

  kms_key_prefix = (
    "arn:${data.aws_partition.current.partition}:kms:${var.service_context.region}:${var.service_context.account_id}:key/"
  )

  registry_arn = (
    "arn:${data.aws_partition.current.partition}:dynamodb:${var.service_context.region}:${var.service_context.account_id}:table/${var.registry.table_name}"
  )

  input_locations = concat(
    [
      {
        bucket_name  = var.configuration_store.bucket_name
        bucket_owner = var.configuration_store.bucket_owner
        prefix       = var.configuration_store.prefix
      },
      {
        bucket_name  = var.published_documents.bucket_name
        bucket_owner = var.published_documents.bucket_owner
        prefix       = var.published_documents.prefix
      },
    ],
    flatten([
      for table in var.tables : concat(
        [
          for location in table.landings : {
            bucket_name  = location.bucket_name
            bucket_owner = location.bucket_owner
            prefix       = location.prefix
          }
        ],
        [
          {
            bucket_name  = table.manifest_location.bucket_name
            bucket_owner = table.manifest_location.bucket_owner
            prefix       = table.manifest_location.prefix
          },
          {
            bucket_name  = table.contract_location.bucket_name
            bucket_owner = table.contract_location.bucket_owner
            prefix       = table.contract_location.prefix
          },
        ]
      )
    ])
  )

  buckets = concat(
    [
      for location in local.input_locations : {
        bucket_name  = location.bucket_name
        bucket_owner = location.bucket_owner
      }
    ],
    [
      {
        bucket_name  = var.history_bucket.bucket_name
        bucket_owner = var.history_bucket.bucket_owner
      },
    ]
  )

  prefixes = concat(
    [for location in local.input_locations : location.prefix],
    [for table in var.tables : table.history_prefix]
  )

  configuration = {
    configuration_version = 1
    context               = var.service_context
    operation_table_name  = local.operations_table_name
    max_batch_bytes       = var.max_batch_bytes

    published_documents = {
      bucket_name  = var.published_documents.bucket_name
      bucket_owner = var.published_documents.bucket_owner
      prefix       = var.published_documents.prefix
    }

    history_bucket = {
      bucket_name  = var.history_bucket.bucket_name
      bucket_owner = var.history_bucket.bucket_owner
    }

    tables = [
      for table in var.tables : {
        schema_name    = table.schema_name
        table_name     = table.table_name
        profile        = table.profile
        history_prefix = table.history_prefix

        landings = [
          for location in table.landings : {
            bucket_name  = location.bucket_name
            bucket_owner = location.bucket_owner
            prefix       = location.prefix
          }
        ]

        manifest_location = {
          bucket_name  = table.manifest_location.bucket_name
          bucket_owner = table.manifest_location.bucket_owner
          prefix       = table.manifest_location.prefix
        }

        contract_location = {
          bucket_name  = table.contract_location.bucket_name
          bucket_owner = table.contract_location.bucket_owner
          prefix       = table.contract_location.prefix
        }
      }
    ]
  }

  configuration_json   = jsonencode(local.configuration)
  configuration_sha256 = sha256(local.configuration_json)
}

resource "terraform_data" "configuration_contract" {
  input = var.function_name

  lifecycle {
    precondition {
      condition = (
        data.aws_caller_identity.current.account_id == var.service_context.account_id &&
        data.aws_region.current.region == var.service_context.region
      )

      error_message = "The AWS provider must match service_context.account_id and service_context.region."
    }

    precondition {
      condition = (
        var.registry.table_arn == local.registry_arn &&
        var.registry.table_name != local.operations_table_name
      )

      error_message = "registry must identify the shared table in the authorised account and region, separate from the operation table."
    }

    precondition {
      condition = (
        var.configuration_store.bucket_owner == var.service_context.account_id
      )

      error_message = "The receiver configuration bucket must belong to the runtime account."
    }

    precondition {
      condition = try(alltrue([
        for bucket in local.buckets :
        can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", bucket.bucket_name)) &&
        can(regex("^[0-9]{12}$", bucket.bucket_owner))
      ]), false)

      error_message = "All locations must identify a bucket and a 12-digit bucket owner."
    }

    precondition {
      condition = try(alltrue([
        for prefix in local.prefixes :
        length(prefix) > 1 &&
        endswith(prefix, "/") &&
        !strcontains(prefix, "*") &&
        !strcontains(prefix, "?") &&
        !strcontains(prefix, "\\") &&
        alltrue([
          for segment in split("/", trimsuffix(prefix, "/")) :
          segment != "" &&
          segment != "." &&
          segment != ".." &&
          segment == trimspace(segment)
        ])
      ]), false)

      error_message = "Locations require explicit relative prefixes ending in '/', without wildcards or empty, '.' or '..' segments."
    }

    precondition {
      condition = (
        length(distinct([
          for table in var.tables :
          jsonencode([table.schema_name, table.table_name])
        ])) == length(var.tables)
      )

      error_message = "Each schema and table combination must be configured once."
    }

    precondition {
      condition = alltrue(flatten([
        for left_index, left in var.tables : [
          for right_index, right in var.tables :
          left_index == right_index || (
            !startswith(left.history_prefix, right.history_prefix) &&
            !startswith(right.history_prefix, left.history_prefix)
          )
        ]
      ]))

      error_message = "Canonical table history prefixes must not overlap."
    }

    precondition {
      condition = alltrue(flatten([
        for location in local.input_locations : [
          for table in var.tables :
          location.bucket_name != var.history_bucket.bucket_name || (
            !startswith(location.prefix, table.history_prefix) &&
            !startswith(table.history_prefix, location.prefix)
          )
        ]
      ]))

      error_message = "History destinations must not overlap configuration, contract, manifest or Raw input locations."
    }

    precondition {
      condition = alltrue([
        for table in var.tables :
        length(distinct([
          for location in table.landings :
          jsonencode([location.bucket_name, location.prefix])
        ])) == length(table.landings)
      ])

      error_message = "A table must not contain duplicate Raw landing locations."
    }

    precondition {
      condition = alltrue([
        for arn in [
          var.configuration_store.kms_key_arn,
          var.operations_kms_key_arn,
        ] :
        startswith(arn, local.kms_key_prefix) &&
        length(trimprefix(arn, local.kms_key_prefix)) > 0 &&
        !strcontains(arn, "*") &&
        !strcontains(arn, "?")
      ])

      error_message = "Configuration and operation-table KMS keys must be explicit keys in the runtime account and region."
    }
  }
}

resource "aws_s3_object" "configuration" {
  bucket = var.configuration_store.bucket_name
  key    = "${var.configuration_store.prefix}${local.configuration_sha256}.json"

  content      = local.configuration_json
  content_type = "application/json"

  source_hash = local.configuration_sha256

  server_side_encryption = "aws:kms"
  kms_key_id             = var.configuration_store.kms_key_arn

  tags = var.tags

  depends_on = [
    terraform_data.configuration_contract,
    aws_dynamodb_table.operations,
  ]

  lifecycle {
    postcondition {
      condition = try(
        self.version_id != null &&
        self.version_id != "" &&
        self.version_id != "null",
        false
      )

      error_message = "The configuration bucket must have versioning enabled."
    }
  }
}
