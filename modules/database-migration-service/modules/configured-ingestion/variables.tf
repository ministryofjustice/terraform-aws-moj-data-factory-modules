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
