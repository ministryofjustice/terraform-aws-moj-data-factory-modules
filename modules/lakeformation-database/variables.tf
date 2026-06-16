variable "database_name" {
  type        = string
  description = "The name of the Glue catalog database to create."

  validation {
    condition     = length(var.database_name) < 50
    error_message = "The database name must be less than 50 characters."
  }
}

variable "storage" {
  type = object(
    {
      bucket_name = string
      prefix      = string
      kms_key_arn = string
    }
  )
}

variable "tags" {
  type        = map(string)
  description = "Tags to apply to created resources."
  default     = {}
}

#766696030771
