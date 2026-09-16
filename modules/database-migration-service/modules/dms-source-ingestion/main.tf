module "dms_core" {
  source = "../dms-core"

  name   = var.name
  vpc_id = var.vpc_id

  replication_instance = {
    replication_instance_id = "${var.name}-dms-instance-${var.environment}"

    replication_instance_class = var.replication_instance.instance_class
    allocated_storage          = var.replication_instance.allocated_storage

    engine_version = var.replication_instance.engine_version
    kms_key_arn    = var.replication_instance.kms_key_arn

    multi_az          = var.replication_instance.multi_az
    availability_zone = var.replication_instance.availability_zone

    apply_immediately            = var.replication_instance.apply_immediately
    auto_minor_version_upgrade   = var.replication_instance.auto_minor_version_upgrade
    preferred_maintenance_window = var.replication_instance.preferred_maintenance_window

    subnet_ids                           = var.network.subnet_ids
    existing_replication_subnet_group_id = var.network.existing_replication_subnet_group_id
    replication_subnet_group_name        = var.network.replication_subnet_group_name
  }

  security_group = {
    allow_all_egress = var.network.allow_all_egress

    additional_vpc_security_group_ids = var.network.additional_security_group_ids
  }

  source_endpoint = {
    endpoint_id = "${var.name}-dms-source-endpoint-${var.environment}"
    engine_name = var.source_endpoint.engine

    database_name = var.source_endpoint.database_name

    secrets_manager_arn             = var.source_endpoint.secret_arn
    secrets_manager_access_role_arn = var.source_endpoint.secret_access_role_arn
    secrets_manager_kms_key_arn     = var.source_endpoint.secret_kms_key_arn

    ssl_mode        = var.source_endpoint.ssl_mode
    certificate_arn = var.source_endpoint.certificate_arn

    extra_connection_attributes = var.source_endpoint.extra_connection_attributes

    postgres_settings = var.source_endpoint.engine == "postgres" ? {
      map_boolean_as_boolean       = var.source_endpoint.postgres == null ? true : var.source_endpoint.postgres.map_boolean_as_boolean
      fail_tasks_on_lob_truncation = var.source_endpoint.postgres == null ? true : var.source_endpoint.postgres.fail_tasks_on_lob_truncation
      heartbeat_enable             = var.source_endpoint.postgres == null ? true : var.source_endpoint.postgres.heartbeat_enable
      heartbeat_frequency          = var.source_endpoint.postgres == null ? 5 : var.source_endpoint.postgres.heartbeat_frequency
    } : null

    oracle_settings = (
      var.source_endpoint.engine == "oracle"
      &&
      var.source_endpoint.oracle != null
      &&
      var.source_endpoint.oracle.asm_secret_arn != null
      ) ? {
      secrets_manager_oracle_asm_secret_arn  = var.source_endpoint.oracle.asm_secret_arn
      secrets_manager_oracle_asm_kms_key_arn = var.source_endpoint.oracle.asm_kms_key_arn
    } : null
  }

  s3_target_endpoint = {
    endpoint_id = "${var.name}-dms-s3-target-endpoint-${var.environment}"

    bucket_name   = var.target.bucket_name
    bucket_folder = var.target.bucket_folder

    service_access_role_arn = var.target.service_access_role_arn

    cdc_max_batch_interval = var.target.cdc.max_batch_interval
    cdc_min_file_size      = var.target.cdc.min_file_size
    cdc_path               = var.target.cdc.path
    max_file_size          = var.target.cdc.max_file_size

    encryption_mode                    = var.target.encryption.mode
    server_side_encryption_kms_key_arn = var.target.encryption.kms_key_arn

    # Platform ingestion conventions
    compression_type                 = "GZIP"
    data_format                      = "parquet"
    include_op_for_full_load         = true
    parquet_timestamp_in_millisecond = false
    timestamp_column_name            = "_timestamp"
  }

  monitoring = {
    enabled = var.monitoring.enabled

    alarm_action_arns             = var.monitoring.alarm_action_arns
    ok_action_arns                = var.monitoring.ok_action_arns
    insufficient_data_action_arns = var.monitoring.insufficient_data_action_arns

    cpu_utilization_threshold          = var.monitoring.cpu_utilization_threshold
    free_storage_space_threshold_bytes = var.monitoring.free_storage_space_threshold_bytes
    freeable_memory_threshold_bytes    = var.monitoring.freeable_memory_threshold_bytes

    period_seconds     = var.monitoring.period_seconds
    evaluation_periods = var.monitoring.evaluation_periods
  }

  replication_tasks = var.replication_tasks

  tags = var.tags
}
