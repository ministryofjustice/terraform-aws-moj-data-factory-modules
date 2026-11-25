output "operations_table" {
  description = "Operation table used for request deduplication and completion recovery."

  value = {
    table_name = aws_dynamodb_table.operations.name
    table_arn  = aws_dynamodb_table.operations.arn
  }
}

output "configuration_reference" {
  description = "Immutable configuration reference used by the receiver Lambda."

  value = {
    bucket_name = var.configuration_store.bucket_name
    object_key  = aws_s3_object.configuration.key
    version_id  = aws_s3_object.configuration.version_id
    sha256      = local.configuration_sha256
  }
}

output "configuration_document" {
  description = "Rendered receiver configuration, also used by module tests."
  value       = local.configuration
}

output "function_arn" {
  description = "Unqualified Raw History Lambda ARN."
  value       = aws_lambda_function.runtime.arn
}

output "qualified_function_arn" {
  description = "Published Lambda version to use in component catalogs."
  value       = aws_lambda_function.runtime.qualified_arn
}

output "function_version" {
  description = "Published Raw History Lambda version."
  value       = aws_lambda_function.runtime.version
}

output "execution_role_arn" {
  description = "Execution role for bucket and KMS policy integration."
  value       = aws_iam_role.runtime.arn
}
