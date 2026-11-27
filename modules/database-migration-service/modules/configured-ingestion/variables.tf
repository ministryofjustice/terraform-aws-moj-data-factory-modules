variable "name_prefix" {
  description = "Prefix for the service runtime Lambda names."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,19}$", var.name_prefix))
    error_message = "name_prefix must be a lowercase identifier of at most 20 characters."
  }
}

variable "compiled_artifacts" {
  description = "Reviewed compiler artifacts indexed by source/extraction-generation."
  type        = any
  nullable    = false

  validation {
    condition = try(
      length(var.compiled_artifacts) > 0 &&
      alltrue([
        for key, artifact in var.compiled_artifacts :
        key == "${artifact.runtime_configuration.source}/${artifact.extraction_generation}" &&
        can(regex(
          "^[a-z][a-z0-9-]{0,29}$",
          artifact.runtime_configuration.source
        )) &&
        artifact.runtime_configuration.environment == "development" &&
        artifact.runtime_configuration.region == "eu-west-2" &&
        alltrue([
          for other in values(var.compiled_artifacts) :
          other.runtime_configuration.source != artifact.runtime_configuration.source ||
          (
            other.runtime_configuration.source_endpoint.database_name == artifact.runtime_configuration.source_endpoint.database_name &&
            other.runtime_configuration.source_endpoint.engine == artifact.runtime_configuration.source_endpoint.engine
          )
        ])
      ]),
      false
    )

    error_message = "Deployment keys must be source/extraction-generation, service identities must remain consistent, and artifacts must target development in eu-west-2."
  }
}

variable "runtime_settings" {
  description = "Platform-owned settings indexed by service name."

  type = map(object({
    published_documents = object({
      bucket_name  = string
      prefix       = string
      bucket_owner = string
      kms_key_arns = optional(set(string), [])
    })

    source_credentials = object({
      mode              = string
      sync_function_arn = optional(string)
    })
  }))

  nullable = false

  validation {
    condition = toset(keys(var.runtime_settings)) == toset([
      for artifact in values(var.compiled_artifacts) :
      artifact.runtime_configuration.source
    ])

    error_message = "Runtime settings must exactly match the distinct configured service names."
  }
}

variable "artifact" {
  description = "Pinned runtime ZIP reference returned by the publishing workflow."

  type = object({
    bucket_name      = string
    object_key       = string
    version_id       = string
    source_code_hash = string
  })

  nullable = false
}

variable "registry" {
  description = "Existing shared Ingestion Registry."

  type = object({
    table_name = string
    table_arn  = string
  })

  nullable = false
}

variable "replication_capacities" {
  description = "Platform-owned capacity bindings indexed by pool name."

  type = map(object({
    vpc_id                         = string
    replication_instance_arn       = string
    replication_instance_id        = string
    replication_security_group_id  = string
    replication_security_group_ids = list(string)
    replication_subnet_group_id    = string

    task_log_group = optional(object({
      arn  = string
      name = string
    }))
  }))

  nullable = false
}

variable "capacity_assignments" {
  description = "Deployment key -> task group -> approved capacity pool name."
  type        = map(map(string))
  nullable    = false

  validation {
    condition = try(
      toset(keys(var.capacity_assignments)) == toset(keys(var.compiled_artifacts)) &&
      alltrue([
        for key, assignments in var.capacity_assignments :
        toset(keys(assignments)) == toset(keys(var.compiled_artifacts[key].module_inputs)) &&
        alltrue([
          for pool in values(assignments) :
          contains(keys(var.replication_capacities), pool)
        ])
      ]),
      false
    )

    error_message = "Each deployment must assign every task group to an existing platform-owned pool."
  }
}

variable "log_retention_days" {
  description = "Runtime log retention in days."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Tags applied to deployed resources."
  type        = map(string)
  default     = {}
}

variable "contract_catalogs" {
  description = "Prepared contract catalogs keyed by source and extraction generation."
  type = map(object({
    catalog_version        = number
    environment            = string
    account_id             = string
    region                 = string
    configuration_revision = string
    artifact_revision      = string

    contracts = object({
      tables = list(object({
        source        = string
        database_name = string
        schema_name   = string
        table_name    = string
        generation    = object({ kind = string, arn = string })
        validation    = object({ kind = string, arn = string })
      }))
      registration = object({ kind = string, arn = string })
      registry     = object({ table_name = string, arn = string })
    })
  }))
  default  = {}
  nullable = false
}

variable "registry_kms_key_arn" {
  description = "KMS key ARN for the shared Ingestion Registry table."
  type        = string
  default     = null
}

variable "raw_batch_handoffs" {
  description = "Optional Raw batch handoff settings keyed by source."

  type = map(object({
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
  }))

  default  = {}
  nullable = false
}

variable "raw_history_runtimes" {
  description = "Optional Raw History deployments keyed by configured service name."

  type = map(object({
    configuration_store = object({
      bucket_name  = string
      bucket_owner = string
      prefix       = string
      kms_key_arn  = string
    })

    history_bucket = object({
      bucket_name  = string
      bucket_owner = string
      kms_key_arn  = string
    })

    operations_kms_key_arn = string

    max_batch_bytes             = optional(number, 8 * 1024 * 1024)
    deletion_protection_enabled = optional(bool, true)

    tables = list(object({
      schema_name    = string
      table_name     = string
      profile        = string
      history_prefix = string

      landings = list(object({
        bucket_name  = string
        bucket_owner = string
        prefix       = string
        kms_key_arns = optional(set(string), [])
      }))

      manifest_location = object({
        bucket_name  = string
        bucket_owner = string
        prefix       = string
        kms_key_arns = optional(set(string), [])
      })

      contract_location = object({
        bucket_name  = string
        bucket_owner = string
        prefix       = string
        kms_key_arns = optional(set(string), [])
      })
    }))
  }))

  default  = {}
  nullable = false

  validation {
    condition = length(setsubtract(
      toset(keys(var.raw_history_runtimes)),
      toset([
        for artifact in values(var.compiled_artifacts) :
        artifact.runtime_configuration.source
      ])
    )) == 0

    error_message = "Raw History deployments must reference configured service names."
  }
}

variable "processing_runtimes" {
  description = "Optional standard Processing runtimes keyed by configured service name."

  type = map(object({
    operations_kms_key_arn = string
    input_bucket_owners = object({
      raw      = string
      manifest = string
      contract = string
    })
    input_locations = map(object({
      bucket_name = string
      prefix      = string
    }))
    input_kms_key_arns = optional(set(string), [])
    output = object({
      bucket_name  = string
      bucket_owner = string
      prefix       = string
    })
    output_kms_key_arn          = optional(string)
    max_batch_bytes             = number
    deletion_protection_enabled = optional(bool, true)
  }))

  default  = {}
  nullable = false
}
