variable "lambda_function_name" {
  description = "Name of the quarantine Lambda function."
  type        = string
  default     = "quarantine-lambda"
}

variable "environment" {
  description = "Environment for the Lambda function."
  type        = string

  validation {
    condition     = contains(["dev", "test", "prod"], var.environment)
    error_message = "Environment must be one of 'dev', 'test', or 'prod'."
  }
}

variable "source_bucket" {
  description = "Output of the s3-bucket module for the source bucket (the bucket GuardDuty scans)."
  type = object({
    bucket      = any
    kms_key_arn = string
  })
}

variable "quarantine_bucket" {
  description = "Output of the s3-bucket module for the quarantine bucket (where malicious objects are moved)."
  type = object({
    bucket      = any
    kms_key_arn = string
  })
}

variable "package_type" {
  description = "Lambda deployment package type. Valid values are Zip or Image."
  type        = string
  default     = "Zip"

  validation {
    condition     = contains(["Zip", "Image"], var.package_type)
    error_message = "package_type must be either 'Zip' or 'Image'."
  }
}

variable "lambda_zip_path" {
  description = "Path to an existing Lambda zip package when package_type is Zip."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.package_type == "Image" || var.lambda_zip_path != null
    error_message = "lambda_zip_path must be provided when package_type is Zip."
  }
}

variable "lambda_image_uri" {
  description = "ECR image URI when package_type is Image."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.package_type == "Zip" || var.lambda_image_uri != null
    error_message = "lambda_image_uri must be provided when package_type is Image."
  }
}

variable "runtime" {
  description = "Lambda runtime used for Zip package type."
  type        = string
  default     = "python3.12"
}

variable "handler" {
  description = "Lambda handler used for Zip package type."
  type        = string
  default     = "lambda_function.lambda_handler"
}

variable "memory_size" {
  description = "Lambda memory size in MB."
  type        = number
  default     = 256
}

variable "timeout" {
  description = "Lambda timeout in seconds."
  type        = number
  default     = 60
}

variable "quarantine_prefix" {
  description = "Prefix to write quarantined objects under in the quarantine bucket."
  type        = string
  default     = "malicious/"
}

variable "action_on_malware" {
  description = "Action for malware events. Valid values are copy or move."
  type        = string
  default     = "copy"
}

variable "eventbridge_rule_name" {
  description = "Name of the EventBridge rule that triggers the quarantine Lambda."
  type        = string
  default     = "eventbridge-guardduty-quarantine"
}

variable "scan_result_statuses" {
  description = "GuardDuty scan result statuses that trigger the quarantine Lambda."
  type        = list(string)
  default     = ["THREATS_FOUND", "FAILED", "ACCESS_DENIED"]
}

variable "tags" {
  description = "Tags to apply to created resources."
  type        = map(string)
  default     = {}
}
