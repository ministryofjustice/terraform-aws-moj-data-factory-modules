# AWS DMS Core Terraform Module

This module contains the reusable AWS DMS infrastructure that sits underneath the
Data Factory `database-migration-service` module.

The aim is to keep the AWS DMS infrastructure separate from the Data Hub-specific
parts of the pipeline such as validation, metadata generation, orchestration and
historical data handling.

The existing `database-migration-service` module currently owns a mixture of these
responsibilities. This core module is being introduced gradually so that we can
create a cleaner reusable DMS boundary without changing or breaking the current
implementation.

## Current scope

At the moment this module creates:

- a DMS replication instance
- a DMS replication subnet group when an existing one is not supplied
- a security group for the replication instance
- the network configuration needed by the replication instance
- a DMS source endpoint supporting PostgreSQL or Oracle
- an S3 target endpoint
- optional least-privilege IAM roles for DMS access to the source secret and S3 target
- configurable CloudWatch alarms for the DMS replication instance
- outputs that can be used by the other DMS components

The source and target endpoints are configuration-driven. The caller provides
the source engine and db configuration, Secrets Manager references and target
S3 configuration.

Existing IAM role ARNs can be supplied by the caller where those roles are
managed outside the module. If they are not supplied the module creates
least-privilege roles for DMS access to the source secret and S3 target.

Monitoring is configurable and notification destinations are supplied by the
caller so that the module does not own environment-specific notification
infrastructure.

## What belongs in this module

The DMS core is intended to own the infrastructure that is generic to an AWS DMS
deployment.

That includes things such as:

- DMS replication infrastructure
- source and target endpoints
- DMS IAM integration
- network integration
- logging and monitoring
- reusable infrastructure outputs

The module can provision and configure DMS resources but it should not decide how
or when the ingestion workflow runs.

## What does not belong in this module

The following responsibilities stay outside the DMS core:

- source and domain-specific configuration
- workflow orchestration
- starting and stopping DMS tasks
- Full Load and CDC sequencing
- retry and recovery decisions
- re-ingestion decisions
- replay
- schema compatibility
- data contract management
- ingestion version allocation
- Raw History versioning and lifecycle
- validation processing
- metadata generation
- Spark processing
- Glue publication
- MI or Analytical Platform processing

These are handled by the other components in the modular ingestion architecture.

## Replication subnet groups

The module supports two ways of configuring the DMS replication subnet group.

An existing subnet group can be supplied using:

`existing_replication_subnet_group_id`

Alternatively at least two subnet IDs can be supplied using:

`subnet_ids`

and the module will create the replication subnet group.

Only one of these approaches should be used.

If the module creates the subnet group `replication_subnet_group_name` can be used
to give it a specific name. If this is not supplied the value of `name` is used.

## Security groups

The module creates a dedicated security group for the DMS replication instance.

By default it keeps the unrestricted outbound behaviour used by the existing DE DMS implementation.
This is intentional for now so that we do not
introduce a different connectivity model while the modular DMS capability is being
built.

Consumers that need a more restricted network setup can disable this behaviour and
attach additional externally managed VPC security groups.

Source-specific connectivity is not hard-coded into the replication instance.
The caller remains responsible for providing the network access required between
DMS and the source db.

## Source endpoint

The module creates a DMS source endpoint for PostgreSQL or Oracle.

The source configuration is supplied by the caller rather than being tied to a
particular Data Hub source or db setup.

Source credentials are referenced through AWS Secrets Manager. The module does
not read or manage the credentials itself.

The caller provides the secret ARN and can optionally provide an existing IAM
role for DMS to access it. If no role is supplied the module creates a
least-privilege role scoped to the supplied secret. A KMS key ARN can also be
supplied where the secret uses a customer-managed KMS key.

Engine-specific DMS connection behaviour can be supplied using
`extra_connection_attributes` where required.

Optional endpoint KMS and certificate references can also be supplied when they
are needed by the source configuration.

The module does not set PostgreSQL or Oracle-specific networking such as database
ports or security-group rules. Those remain part of the caller's network and
source configuration.

## S3 target endpoint

The module creates the DMS S3 target endpoint used for landing replicated data.

The caller provides the target bucket and can optionally provide an existing
IAM role for DMS to access it. If no role is supplied the module creates a
least-privilege role scoped to the supplied bucket or bucket folder.

The endpoint supports configurable DMS S3 settings including output format,
compression, Parquet settings, CDC batching and encryption.

S3 target encryption can use either S3-managed encryption or KMS-backed
encryption. When KMS-backed encryption is used the caller provides the required
KMS key reference.

The module only configures the DMS target endpoint. It does not create or own the
target bucket and it does not manage Raw History, ingestion versions or any
downstream data lifecycle.

## DMS engine version

The module does not keep a hard-coded list of allowed DMS engine versions.

The existing `database-migration-service` module currently has a fixed list which
means the module needs to be changed whenever a newer supported DMS version is
required.

For the core module the consumer can provide an `engine_version` supported by AWS
DMS and the AWS provider.

If `engine_version` is not supplied the AWS provider and DMS defaults are used.

## IAM

The module can create the IAM roles required by DMS to access the configured
source secret and S3 target.

For source Secrets Manager access the generated role is scoped to the supplied
secret. If a customer-managed KMS key ARN is supplied for the secret the role
is also granted the required KMS permissions.

For S3 target access the generated role is scoped to the supplied target bucket
and when configured the supplied bucket folder. If KMS-backed target encryption
is configured the role is also granted the required permissions on that key.

Consumers can provide existing IAM role ARNs instead. In that case the module
uses those roles rather than creating new ones.

The account-level DMS service roles are intentionally not created by this module,
including:

- `dms-vpc-role`
- `dms-cloudwatch-logs-role`

These roles have an account-level lifecycle and may be shared by multiple DMS
deployments so they remain outside an individual `dms-core` deployment.

## Monitoring

The module can create CloudWatch alarms for the DMS replication instance.

The current monitoring covers:

- CPU utilization
- free storage space
- freeable memory

Monitoring is enabled by default and the thresholds, evaluation period and alarm
period can be configured by the caller.

Alarm, OK and insufficient-data action ARNs are also supplied by the caller.
The module therefore creates the reusable DMS monitoring capability without
owning SNS topics, Slack integrations or other environment-specific notification
infrastructure.

Task-specific monitoring and workflow-specific alerting remain outside the
DMS core module.

## Backwards compatibility

This core module is being added alongside the existing implementation.

Nothing in the current `database-migration-service` module has been moved into this
module yet and existing consumers are not being changed to use it as part of this
initial work.

That means the existing Terraform resource addresses and deployed DMS resources
remain unchanged.

In particular this work does not change the existing:

- replication instances
- source or target endpoints
- replication tasks
- S3 buckets
- metadata generation
- validation infrastructure
- IAM resources
- monitoring or notifications

If existing resources are moved into the core module in the future that will need
to be treated as a separate migration.

Before doing that we will need to check the Terraform state and plans carefully and
make sure the change does not unexpectedly replace, destroy or alter existing
infrastructure.

## Terraform reference

The inputs, outputs, providers and resources below are generated from the Terraform
configuration using `terraform-docs`.

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
| ---- | ------- |
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.3.0, < 2.0.0 |
| <a name="requirement_aws"></a> [aws](#requirement\_aws) | ~> 6.42 |

## Providers

| Name | Version |
| ---- | ------- |
| <a name="provider_aws"></a> [aws](#provider\_aws) | 6.62.0 |

## Modules

No modules.

## Resources

| Name | Type |
| ---- | ---- |
| [aws_cloudwatch_metric_alarm.replication_instance_cpu_utilization](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_metric_alarm) | resource |
| [aws_cloudwatch_metric_alarm.replication_instance_free_storage_space](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_metric_alarm) | resource |
| [aws_cloudwatch_metric_alarm.replication_instance_freeable_memory](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_metric_alarm) | resource |
| [aws_dms_endpoint.source](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/dms_endpoint) | resource |
| [aws_dms_replication_instance.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/dms_replication_instance) | resource |
| [aws_dms_replication_subnet_group.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/dms_replication_subnet_group) | resource |
| [aws_dms_s3_endpoint.target](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/dms_s3_endpoint) | resource |
| [aws_iam_role.s3_target_access](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role.source_secrets_access](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role_policy.s3_target_access](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_iam_role_policy.source_secrets_access](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_security_group.replication_instance](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/security_group) | resource |
| [aws_vpc_security_group_egress_rule.replication_instance](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/vpc_security_group_egress_rule) | resource |
| [aws_iam_policy_document.dms_assume_role](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.s3_target_access](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.source_secrets_access](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |

## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| <a name="input_monitoring"></a> [monitoring](#input\_monitoring) | CloudWatch monitoring configuration for the DMS replication instance.<br/><br/>Monitoring is enabled by default. Alarm destinations are supplied by the<br/>caller so this module does not own SNS topics, Slack integrations or other<br/>notification infrastructure.<br/><br/>Thresholds are configurable to avoid embedding environment-specific<br/>policy in the reusable module. | <pre>object({<br/>    enabled = optional(bool, true)<br/><br/>    alarm_action_arns             = optional(list(string), [])<br/>    ok_action_arns                = optional(list(string), [])<br/>    insufficient_data_action_arns = optional(list(string), [])<br/><br/>    cpu_utilization_threshold          = optional(number, 80)<br/>    free_storage_space_threshold_bytes = optional(number, 10737418240)<br/>    freeable_memory_threshold_bytes    = optional(number, 1073741824)<br/><br/>    period_seconds     = optional(number, 300)<br/>    evaluation_periods = optional(number, 3)<br/>  })</pre> | `{}` | no |
| <a name="input_name"></a> [name](#input\_name) | Stable name used to identify the DMS ingestion infrastructure. | `string` | n/a | yes |
| <a name="input_replication_instance"></a> [replication\_instance](#input\_replication\_instance) | Configuration for the AWS DMS replication instance.<br/><br/>For replication subnet configuration exactly one of the following approaches<br/>must be used:<br/><br/>  - provide existing\_replication\_subnet\_group\_id to use an existing DMS<br/>    replication subnet group<br/><br/>    or<br/><br/>  - provide at least two subnet\_ids and allow this module to create the DMS<br/>    replication subnet group<br/><br/>replication\_subnet\_group\_name is used only when this module creates the subnet<br/>group. If omitted name is used.<br/><br/>engine\_version is intentionally not restricted to a hard-coded allow list.<br/>AWS DMS and the AWS provider remain authoritative for supported engine versions. | <pre>object({<br/>    replication_instance_id    = string<br/>    replication_instance_class = string<br/>    allocated_storage          = number<br/><br/>    engine_version = optional(string)<br/>    kms_key_arn    = optional(string)<br/><br/>    multi_az          = optional(bool, false)<br/>    availability_zone = optional(string)<br/><br/>    apply_immediately            = optional(bool, false)<br/>    auto_minor_version_upgrade   = optional(bool, true)<br/>    preferred_maintenance_window = optional(string, "sun:10:30-sun:14:30")<br/><br/>    existing_replication_subnet_group_id = optional(string)<br/>    replication_subnet_group_name        = optional(string)<br/>    subnet_ids                           = optional(list(string))<br/>  })</pre> | n/a | yes |
| <a name="input_s3_target_endpoint"></a> [s3\_target\_endpoint](#input\_s3\_target\_endpoint) | Configuration for the AWS DMS S3 target endpoint.<br/><br/>The target bucket is supplied by the caller.<br/>The caller may provide an existing DMS service-access role. If no role is<br/>supplied the module creates a least-privilege role for AWS DMS to access<br/>the target bucket.<br/><br/>This module configures DMS to write to the supplied S3 landing location but<br/>does not create or manage the wider Raw/Raw History storage lifecycle.<br/><br/>S3 target settings are configurable so consumers can override DMS defaults<br/>without introducing Data Hub-specific assumptions into the reusable module. | <pre>object({<br/>    endpoint_id             = string<br/>    bucket_name             = string<br/>    service_access_role_arn = optional(string)<br/><br/>    bucket_folder = optional(string)<br/><br/>    add_column_name        = optional(bool, true)<br/>    cdc_max_batch_interval = optional(number, 3600)<br/>    cdc_min_file_size      = optional(number, 32000)<br/><br/>    compression_type = optional(string, "GZIP")<br/>    data_format      = optional(string, "parquet")<br/>    encoding_type    = optional(string, "rle_dictionary")<br/><br/>    encryption_mode                    = optional(string, "SSE_S3")<br/>    server_side_encryption_kms_key_arn = optional(string)<br/><br/>    include_op_for_full_load         = optional(bool, true)<br/>    parquet_timestamp_in_millisecond = optional(bool, true)<br/>    parquet_version                  = optional(string, "parquet-2-0")<br/>    timestamp_column_name            = optional(string, "EXTRACTION_TIMESTAMP")<br/>  })</pre> | n/a | yes |
| <a name="input_security_group"></a> [security\_group](#input\_security\_group) | Security-group configuration for the DMS replication instance.<br/><br/>This module always creates a dedicated security group for the replication instance.<br/><br/>allow\_all\_egress defaults to true to preserve the connectivity behaviour<br/>of the existing DE DMS implementation.<br/><br/>Consumers with a stricter network model can disable that rule and attach<br/>additional externally managed VPC security groups using<br/>additional\_vpc\_security\_group\_ids.<br/><br/>Source-specific ingress/egress policy is intentionally not modelled by this<br/>module. Consumers remain responsible for providing the network connectivity<br/>required between the DMS replication instance and the configured source. | <pre>object({<br/>    allow_all_egress                  = optional(bool, true)<br/>    additional_vpc_security_group_ids = optional(list(string), [])<br/>  })</pre> | `{}` | no |
| <a name="input_source_endpoint"></a> [source\_endpoint](#input\_source\_endpoint) | Configuration for the AWS DMS source endpoint.<br/><br/>The source endpoint supports PostgreSQL and Oracle.<br/><br/>Authentication is provided through AWS Secrets Manager.<br/>The caller supplies the secret ARN and may provide an existing<br/>IAM role ARN for AWS DMS to use.<br/><br/>If no access role is supplied the module creates a least-privilege role for<br/>AWS DMS to access the source secret.<br/><br/>The module does not read or decode the secret contents itself.<br/><br/>database\_name remains explicit because it is part of the DMS endpoint<br/>configuration rather than a credential.<br/><br/>Engine-specific DMS behaviour can be supplied through<br/>extra\_connection\_attributes where required without embedding Data Hub-specific<br/>assumptions into this module. | <pre>object({<br/>    endpoint_id = string<br/>    engine_name = string<br/><br/>    database_name = string<br/><br/>    secrets_manager_arn             = string<br/>    secrets_manager_access_role_arn = optional(string)<br/>    secrets_manager_kms_key_arn     = optional(string)<br/><br/>    kms_key_arn     = optional(string)<br/>    certificate_arn = optional(string)<br/><br/>    ssl_mode                    = optional(string, "none")<br/>    extra_connection_attributes = optional(string)<br/>  })</pre> | n/a | yes |
| <a name="input_tags"></a> [tags](#input\_tags) | Tags applied to resources created by this module. | `map(string)` | `{}` | no |
| <a name="input_vpc_id"></a> [vpc\_id](#input\_vpc\_id) | VPC in which the DMS replication infrastructure is deployed. | `string` | n/a | yes |

## Outputs

| Name | Description |
| ---- | ----------- |
| <a name="output_replication_instance_arn"></a> [replication\_instance\_arn](#output\_replication\_instance\_arn) | ARN of the DMS replication instance. |
| <a name="output_replication_instance_id"></a> [replication\_instance\_id](#output\_replication\_instance\_id) | Identifier of the DMS replication instance. |
| <a name="output_replication_security_group_id"></a> [replication\_security\_group\_id](#output\_replication\_security\_group\_id) | ID of the security group created by this module for the DMS replication instance. |
| <a name="output_replication_security_group_ids"></a> [replication\_security\_group\_ids](#output\_replication\_security\_group\_ids) | Security group IDs attached to the DMS replication instance. |
| <a name="output_replication_subnet_group_id"></a> [replication\_subnet\_group\_id](#output\_replication\_subnet\_group\_id) | ID of the DMS replication subnet group used by the replication instance. |
| <a name="output_s3_target_service_access_role_arn"></a> [s3\_target\_service\_access\_role\_arn](#output\_s3\_target\_service\_access\_role\_arn) | ARN of the IAM role used by AWS DMS to access the S3 target. |
| <a name="output_source_endpoint_arn"></a> [source\_endpoint\_arn](#output\_source\_endpoint\_arn) | ARN of the DMS source endpoint. |
| <a name="output_source_endpoint_id"></a> [source\_endpoint\_id](#output\_source\_endpoint\_id) | Identifier of the DMS source endpoint. |
| <a name="output_source_secrets_manager_access_role_arn"></a> [source\_secrets\_manager\_access\_role\_arn](#output\_source\_secrets\_manager\_access\_role\_arn) | ARN of the IAM role used by AWS DMS to access the source secret. |
| <a name="output_target_endpoint_arn"></a> [target\_endpoint\_arn](#output\_target\_endpoint\_arn) | ARN of the DMS S3 target endpoint. |
| <a name="output_target_endpoint_id"></a> [target\_endpoint\_id](#output\_target\_endpoint\_id) | Identifier of the DMS S3 target endpoint. |
<!-- END_TF_DOCS -->
