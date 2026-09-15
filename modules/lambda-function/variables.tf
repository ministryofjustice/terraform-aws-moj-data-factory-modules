variable "name" {
  type        = string
  description = "The name of the lambda function"
}

variable "lambda_path" {
  type        = string
  description = "The path to the lambda function directory"
}

variable "region" {
  type        = string
  default     = "eu-west-2"
  description = "The AWS region to deploy the lambda function"
}

variable "vulnerability_scanner_threshold" {
  type        = string
  description = "The threshold for the vulnerability scanner"
  default     = "high"

  validation {
    condition     = contains(["negligible", "low", "medium", "high", "critical"], var.vulnerability_scanner_threshold)
    error_message = "Invalid vulnerability scanner threshold. Must be one of: negligible, low, medium, high, critical."
  }
}
