module "capacity" {
  count = var.existing_replication_capacity == null ? 1 : 0

  source = "../dms-replication-capacity"

  name                 = var.name
  vpc_id               = var.vpc_id
  replication_instance = var.replication_instance
  security_group       = var.security_group
  monitoring           = var.monitoring
  task_logging         = var.task_logging

  create_task_log_group = length(var.replication_tasks) > 0

  tags = var.tags
}

locals {
  replication_capacity = (
    var.existing_replication_capacity == null
    ? module.capacity[0].binding
    : var.existing_replication_capacity
  )

  replication_security_group_ids = local.replication_capacity.replication_security_group_ids
  replication_subnet_group_id    = local.replication_capacity.replication_subnet_group_id
}
