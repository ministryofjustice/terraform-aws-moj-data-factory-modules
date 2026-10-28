variable "compiled_artifact" {
  description = <<-EOT
    A service artifact produced by ingestion-config.

    The service YAML and platform bindings must be validated and compiled
    before this module is called. Runtime ingestion versions and execution
    details do not belong in this input.
  EOT

  type = any

  validation {
    condition = try(
      var.compiled_artifact.artifact_version == 1
      &&
      var.compiled_artifact.runtime_configuration.environment == "development"
      &&
      var.compiled_artifact.runtime_configuration.region == "eu-west-2"
      &&
      length(var.compiled_artifact.module_inputs) > 0
      &&
      toset(keys(var.compiled_artifact.module_inputs))
      == toset(keys(var.compiled_artifact.runtime_configuration.task_groups))
      &&
      alltrue([
        for inputs in values(var.compiled_artifact.module_inputs) :
        inputs.environment == "development"
      ]),
      false
    )

    error_message = "compiled_artifact must be a version 1 development artifact for eu-west-2 with matching, non-empty task groups."
  }
}

variable "tags" {
  description = "Tags applied to the service's DMS resources."
  type        = map(string)
  default     = {}
}

variable "replication_capacities" {
  description = "Optional platform-owned capacity bindings indexed by task group. Empty preserves dedicated mode."

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

  default = {}

  validation {
    condition = length(setsubtract(
      toset(keys(var.replication_capacities)),
      toset(keys(var.compiled_artifact.module_inputs))
    )) == 0

    error_message = "Capacity bindings may only reference configured task groups."
  }
}
