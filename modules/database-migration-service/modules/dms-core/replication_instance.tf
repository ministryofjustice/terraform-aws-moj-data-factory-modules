locals {
  create_replication_subnet_group = (
    var.replication_instance.existing_replication_subnet_group_id == null
  )

  replication_subnet_group_name = coalesce(
    var.replication_instance.replication_subnet_group_name,
    var.name
  )

  replication_subnet_group_id = (
    local.create_replication_subnet_group
    ? aws_dms_replication_subnet_group.this[0].id
    : var.replication_instance.existing_replication_subnet_group_id
  )

  replication_security_group_ids = concat(
    [aws_security_group.replication_instance.id],
    var.security_group.additional_vpc_security_group_ids
  )
}

resource "aws_security_group" "replication_instance" {
  name        = "${var.name}-dms-replication-instance"
  description = "Security group for the DMS replication instance managed by Terraform."
  vpc_id      = var.vpc_id

  tags = merge(
    var.tags,
    {
      Name = "${var.name}-dms-replication-instance"
    }
  )
}

# Broad outbound connectivity preserves the behaviour of the existing DE DMS implementation.
#
# Consumers with a stricter network model can disable this rule and attach externally managed security groups/rules instead.
#
# Source-specific connectivity will be addressed as part of the reusable
# endpoint/network integration rather than being hard-coded into the replication infrastructure.
#
# trivy:ignore:aws-vpc-no-public-egress-sgr

resource "aws_vpc_security_group_egress_rule" "replication_instance" {
  count = var.security_group.allow_all_egress ? 1 : 0

  security_group_id = aws_security_group.replication_instance.id
  description       = "Allow outbound traffic from the DMS replication instance."

  cidr_ipv4   = "0.0.0.0/0"
  ip_protocol = "-1"

  tags = var.tags
}

resource "aws_dms_replication_subnet_group" "this" {
  count = local.create_replication_subnet_group ? 1 : 0

  replication_subnet_group_id          = local.replication_subnet_group_name
  replication_subnet_group_description = "Subnet group for ${var.name} DMS replication infrastructure."
  subnet_ids                           = var.replication_instance.subnet_ids

  tags = merge(
    var.tags,
    {
      Name = local.replication_subnet_group_name
    }
  )
}

resource "aws_dms_replication_instance" "this" {
  replication_instance_id    = var.replication_instance.replication_instance_id
  replication_instance_class = var.replication_instance.replication_instance_class
  allocated_storage          = var.replication_instance.allocated_storage

  engine_version             = var.replication_instance.engine_version
  auto_minor_version_upgrade = var.replication_instance.auto_minor_version_upgrade

  kms_key_arn = var.replication_instance.kms_key_arn

  multi_az          = var.replication_instance.multi_az
  availability_zone = var.replication_instance.availability_zone

  apply_immediately            = var.replication_instance.apply_immediately
  preferred_maintenance_window = var.replication_instance.preferred_maintenance_window

  publicly_accessible = false

  replication_subnet_group_id = local.replication_subnet_group_id
  vpc_security_group_ids      = local.replication_security_group_ids

  tags = merge(
    var.tags,
    {
      Name = var.replication_instance.replication_instance_id
    }
  )
}
