output "name" {
  description = "Resolved name value"
  value       = var.name
}

output "environment" {
  description = "Resolved environment value"
  value       = var.environment
}

output "tags" {
  description = "Merged tag map"
  value       = local.tags
}
