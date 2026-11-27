resource "terraform_data" "processing_contract" {
  for_each = var.processing_runtimes

  input = each.key

  lifecycle {
    precondition {
      condition     = contains(local.services, each.key)
      error_message = "Each Processing runtime must reference a configured service."
    }

    precondition {
      condition = anytrue(flatten([
        for artifact in values(var.compiled_artifacts) : [
          for group in values(artifact.runtime_configuration.task_groups) : [
            for table in group.tables :
            table.processing_profile == "standard"
          ]
        ]
        if artifact.runtime_configuration.source == each.key
      ]))
      error_message = "Each Processing runtime requires a table with the standard Processing profile in its compiled service configuration."
    }
  }
}

module "processing" {
  for_each = var.processing_runtimes

  source = "../processing-runtime"

  function_name = "${var.name_prefix}-${each.key}-processing"
  artifact      = var.artifact

  operations_kms_key_arn      = each.value.operations_kms_key_arn
  deletion_protection_enabled = each.value.deletion_protection_enabled

  input_bucket_owners = each.value.input_bucket_owners
  input_locations     = each.value.input_locations
  input_kms_key_arns  = each.value.input_kms_key_arns

  output             = each.value.output
  output_kms_key_arn = each.value.output_kms_key_arn
  max_batch_bytes    = each.value.max_batch_bytes

  log_retention_days = var.log_retention_days
  tags               = var.tags

  depends_on = [terraform_data.processing_contract]
}
