resource "aws_dms_endpoint" "source" {
  endpoint_id   = var.source_endpoint.endpoint_id
  endpoint_type = "source"
  engine_name   = var.source_endpoint.engine_name

  database_name = var.source_endpoint.database_name

  kms_key_arn     = var.source_endpoint.kms_key_arn
  certificate_arn = var.source_endpoint.certificate_arn

  secrets_manager_arn             = var.source_endpoint.secrets_manager_arn
  secrets_manager_access_role_arn = local.source_secrets_manager_access_role_arn

  ssl_mode                    = var.source_endpoint.ssl_mode
  extra_connection_attributes = var.source_endpoint.extra_connection_attributes

  tags = merge(
    var.tags,
    {
      Name = var.source_endpoint.endpoint_id
    }
  )
}

resource "aws_dms_s3_endpoint" "target" {
  endpoint_id   = var.s3_target_endpoint.endpoint_id
  endpoint_type = "target"

  bucket_name             = var.s3_target_endpoint.bucket_name
  bucket_folder           = var.s3_target_endpoint.bucket_folder
  service_access_role_arn = local.s3_target_service_access_role_arn

  add_column_name        = var.s3_target_endpoint.add_column_name
  cdc_max_batch_interval = var.s3_target_endpoint.cdc_max_batch_interval
  cdc_min_file_size      = var.s3_target_endpoint.cdc_min_file_size

  compression_type = var.s3_target_endpoint.compression_type
  data_format      = var.s3_target_endpoint.data_format
  encoding_type    = var.s3_target_endpoint.encoding_type

  encryption_mode                   = var.s3_target_endpoint.encryption_mode
  server_side_encryption_kms_key_id = var.s3_target_endpoint.server_side_encryption_kms_key_arn

  include_op_for_full_load         = var.s3_target_endpoint.include_op_for_full_load
  parquet_timestamp_in_millisecond = var.s3_target_endpoint.parquet_timestamp_in_millisecond
  parquet_version                  = var.s3_target_endpoint.parquet_version
  timestamp_column_name            = var.s3_target_endpoint.timestamp_column_name

  tags = merge(
    var.tags,
    {
      Name = var.s3_target_endpoint.endpoint_id
    }
  )
}