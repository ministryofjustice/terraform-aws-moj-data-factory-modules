variable "aws_region" {
  description = "AWS region used by the example."
  type        = string
  default     = "eu-west-2"
}

variable "name" {
  description = "Stable name used for the example DMS infrastructure."
  type        = string
  default     = "example-dms-core"
}

variable "vpc_id" {
  description = "Existing VPC ID in which the DMS replication infrastructure will be created."
  type        = string
}

variable "subnet_ids" {
  description = "Existing subnet IDs used to create the DMS replication subnet group."
  type        = list(string)
}

variable "replication_instance_id" {
  description = "Identifier for the example DMS replication instance."
  type        = string
  default     = "example-dms-core"
}

variable "replication_instance_class" {
  description = "AWS DMS replication instance class."
  type        = string
  default     = "dms.t3.medium"
}

variable "allocated_storage" {
  description = "Storage allocated to the DMS replication instance in GiB."
  type        = number
  default     = 50
}

variable "engine_version" {
  description = "Optional AWS DMS engine version."
  type        = string
  default     = null
}

variable "kms_key_arn" {
  description = "Optional KMS key ARN used to encrypt the DMS replication instance."
  type        = string
  default     = null
}

variable "multi_az" {
  description = "Whether the DMS replication instance should use Multi-AZ."
  type        = bool
  default     = false
}

variable "availability_zone" {
  description = "Optional Availability Zone for a single-AZ DMS replication instance."
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags applied to resources created by the example."
  type        = map(string)

  default = {
    application = "dms-core-example"
  }
}
