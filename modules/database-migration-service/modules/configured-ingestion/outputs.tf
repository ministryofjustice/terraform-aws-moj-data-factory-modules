output "deployments" {
  description = "Compiled artifacts, deployed bindings and runtime references indexed by source/extraction-generation."
  sensitive   = true

  value = {
    for key, artifact in var.compiled_artifacts : key => {
      compiled_artifact   = artifact
      deployment_bindings = module.service[key].deployment_bindings

      runtime = {
        function_name = module.runtime[
          artifact.runtime_configuration.source
        ].function_name

        function_arn = module.runtime[
          artifact.runtime_configuration.source
        ].function_arn

        execution_role_arn = module.runtime[
          artifact.runtime_configuration.source
        ].execution_role_arn

        log_group_name = module.runtime[
          artifact.runtime_configuration.source
        ].log_group_name
      }
    }
  }
}

output "retained_inventory" {
  description = "Reviewed compiler artifacts to retain unchanged when preparing another generation."
  sensitive   = true

  value = {
    compiled_artifacts = var.compiled_artifacts
  }
}
