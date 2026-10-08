locals {
  generation_environment = merge(
    {
      DB_SECRET_ARN          = var.source_config.secret_arn
      SCHEMA_REGISTRY_BUCKET = var.schema_registry_bucket_name
      ENGINE                 = var.source_config.engine
      CONTRACT_NAMESPACE     = var.contract_namespace
      CONTRACT_SERVICE       = var.source_config.service
      DATABASE_NAME          = var.source_config.database_name
      SCHEMA_NAME            = var.source_config.schema_name
      CONTRACT_VERSION       = var.contract_version
      TABLE_LIST             = join(",", var.source_config.table_names)
      CONTRACT_CONTACTS      = jsonencode(var.contract_contacts)
    },
    var.source_config.oracle_service_name == null ? {} : {
      DATABASE_SERVICE_NAME = var.source_config.oracle_service_name
    },
    var.source_config.oracle_sid == null ? {} : {
      DATABASE_SID = var.source_config.oracle_sid
    },
  )
}
