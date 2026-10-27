output "deployment_bindings" {
  description = "DMS resource bindings keyed by task group."

  value = {
    for group_name, deployment in module.source_ingestion :
    group_name => deployment.deployment_binding
  }
}

output "replication_tasks" {
  description = "DMS replication tasks keyed by task group."

  value = {
    for group_name, deployment in module.source_ingestion :
    group_name => deployment.replication_tasks
  }
}

output "configuration_revision" {
  description = "Configuration revision supplied by the compiler."
  value       = var.compiled_artifact.configuration_revision
}

output "artifact_revision" {
  description = "Artifact revision supplied by the compiler."
  value       = var.compiled_artifact.artifact_revision
}

output "extraction_generation" {
  description = "Extraction generation supplied by the compiler."
  value       = var.compiled_artifact.extraction_generation
}
