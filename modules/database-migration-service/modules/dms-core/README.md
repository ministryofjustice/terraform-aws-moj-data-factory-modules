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
- outputs that can be used by the other DMS components

The next pieces of work will add the reusable source and target endpoints, IAM and
monitoring behaviour.

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

Source-specific connectivity will be handled alongside the reusable endpoint and
network configuration rather than being hard-coded into the replication instance.

## DMS engine version

The module does not keep a hard-coded list of allowed DMS engine versions.

The existing `database-migration-service` module currently has a fixed list which
means the module needs to be changed whenever a newer supported DMS version is
required.

For the core module the consumer can provide an `engine_version` supported by AWS
DMS and the AWS provider.

If `engine_version` is not supplied the AWS provider and DMS defaults are used.

## IAM prerequisites

This first part of the module does not create the account-level DMS service roles including:

- `dms-vpc-role`
- `dms-cloudwatch-logs-role`

Those roles have a different lifecycle from an individual DMS ingestion and can be
shared by more than one pipeline in an AWS account.

We will deal with the IAM boundary separately rather than coupling those account-level
roles directly to every replication instance.

For now any AWS account using this module must already have the DMS prerequisites
required by AWS.

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
| <a name="provider_aws"></a> [aws](#provider\_aws) | ~> 6.42 |

## Modules

No modules.

## Resources

| Name | Type |
| ---- | ---- |
| [aws_dms_replication_instance.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/dms_replication_instance) | resource |
| [aws_dms_replication_subnet_group.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/dms_replication_subnet_group) | resource |
| [aws_security_group.replication_instance](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/security_group) | resource |
| [aws_vpc_security_group_egress_rule.replication_instance](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/vpc_security_group_egress_rule) | resource |

## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| <a name="input_name"></a> [name](#input\_name) | Stable name used to identify the DMS ingestion infrastructure. | `string` | n/a | yes |
| <a name="input_replication_instance"></a> [replication\_instance](#input\_replication\_instance) | Configuration for the AWS DMS replication instance.<br/><br/>For replication subnet configuration exactly one of the following approaches<br/>must be used:<br/><br/>  - provide existing\_replication\_subnet\_group\_id to use an existing DMS<br/>    replication subnet group<br/><br/>    or<br/><br/>  - provide at least two subnet\_ids and allow this module to create the DMS<br/>    replication subnet group<br/><br/>replication\_subnet\_group\_name is used only when this module creates the subnet<br/>group. If omitted name is used.<br/><br/>engine\_version is intentionally not restricted to a hard-coded allow list.<br/>AWS DMS and the AWS provider remain authoritative for supported engine versions. | <pre>object({<br/>    replication_instance_id    = string<br/>    replication_instance_class = string<br/>    allocated_storage          = number<br/><br/>    engine_version = optional(string)<br/>    kms_key_arn    = optional(string)<br/><br/>    multi_az          = optional(bool, false)<br/>    availability_zone = optional(string)<br/><br/>    apply_immediately            = optional(bool, false)<br/>    auto_minor_version_upgrade   = optional(bool, true)<br/>    preferred_maintenance_window = optional(string, "sun:10:30-sun:14:30")<br/><br/>    existing_replication_subnet_group_id = optional(string)<br/>    replication_subnet_group_name        = optional(string)<br/>    subnet_ids                           = optional(list(string))<br/>  })</pre> | n/a | yes |
| <a name="input_security_group"></a> [security\_group](#input\_security\_group) | Security-group configuration for the DMS replication instance.<br/><br/>This module always creates a dedicated security group for the replication instance.<br/><br/>allow\_all\_egress defaults to true to preserve the connectivity behaviour<br/>of the existing DE DMS implementation.<br/><br/>Consumers with a stricter network model can disable that rule and attach<br/>additional externally managed VPC security groups using<br/>additional\_vpc\_security\_group\_ids.<br/><br/>Source-specific ingress/egress policy is intentionally not modelled here yet.<br/>That boundary will be addressed alongside reusable endpoint/network integration. | <pre>object({<br/>    allow_all_egress                  = optional(bool, true)<br/>    additional_vpc_security_group_ids = optional(list(string), [])<br/>  })</pre> | `{}` | no |
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
<!-- END_TF_DOCS -->
