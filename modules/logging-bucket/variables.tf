variable "force_destroy" {
  description = "Whether Terraform can delete the logging bucket when it contains objects. Keep false for production audit logs."
  type        = bool
  default     = false
}
