locals {
  validation_table_name = "${var.name}-validation-audit"

  validation_environment = {
    SCHEMA_AUDIT_TABLE_NAME     = var.registry_table_name
    VALIDATION_AUDIT_TABLE_NAME = local.validation_table_name
    INGESTION_TYPE              = var.ingestion_type
    SOURCE_MAPPINGS             = jsonencode(var.source_mappings)
  }

  incoming_object_arns = toset([
    for mapping in var.source_mappings :
    "arn:${data.aws_partition.current.partition}:s3:::${mapping.bucket_name}/${mapping.prefix}*"
  ])

  s3_read_kms_key_arns = setunion(
    var.incoming_kms_key_arns,
    var.contract_kms_key_arns,
  )

  registry_table_name_from_arn = element(
    split("/", var.registry_table_arn),
    length(split("/", var.registry_table_arn)) - 1,
  )

  validation_environment_size_bytes = sum([
    for key, value in local.validation_environment :
    (
      length(base64encode(key)) * 3 / 4 -
      length(regexall("=", base64encode(key)))
    ) +
    (
      length(base64encode(value)) * 3 / 4 -
      length(regexall("=", base64encode(value)))
    )
  ])
}
