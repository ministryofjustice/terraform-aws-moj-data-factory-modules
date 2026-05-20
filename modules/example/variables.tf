variable "name" {
  description = "Name used for tagging and module outputs"
  type        = string

  validation {
    condition     = length(trimspace(var.name)) > 0
    error_message = "name must not be empty."
  }
}

variable "environment" {
  description = "Environment identifier (for example: dev, test, preprod, prod)"
  type        = string

  validation {
    condition     = contains(["dev", "test", "preprod", "prod"], var.environment)
    error_message = "environment must be one of: dev, test, preprod, prod."
  }
}

variable "additional_tags" {
  description = "Extra tags to merge with the module defaults"
  type        = map(string)
  default     = {}
}
