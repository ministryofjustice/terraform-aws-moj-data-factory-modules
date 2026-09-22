# AWS DMS Source Ingestion Terraform Module

This module provides the generic consumer-facing interface for provisioning AWS
DMS source-ingestion infrastructure.

It configures the reusable `dms-core` module while keeping service-specific and
environment-specific configuration outside the shared implementation.

The same module can be used for development, pre-production and production by
supplying the appropriate configuration for each environment.

## Current scope

At the moment this module configures:

- a DMS replication instance
- DMS network and subnet configuration
- additional security-group integration
- a PostgreSQL or Oracle source endpoint
- Secrets Manager authentication for the source endpoint
- optional Oracle ASM credential references
- an S3 target endpoint
- target encryption
- Full Load, CDC and Full Load + CDC replication tasks
- CloudWatch monitoring
- CloudWatch task logging
- reusable outputs for orchestration and other platform components

The module exposes a generic source-ingestion contract and delegates the
low-level AWS DMS resource creation to `dms-core`.

## What belongs in this module

This module owns the reusable configuration boundary used by services that need
to ingest data through AWS DMS.

That includes:

- selecting the supported source engine
- mapping source configuration into `dms-core`
- passing source secret references to DMS
- passing engine-specific endpoint settings to DMS
- configuring the DMS replication instance
- configuring the S3 landing target
- supplying replication-task definitions
- applying common platform ingestion conventions
- exposing DMS resource identifiers for orchestration

The module should remain independent of any individual service, database or
deployment environment.

## What does not belong in this module

The following responsibilities remain outside this module:

- creating or managing the source database
- creating source database users
- changing source database parameters
- preparing a source database for CDC
- creating source-specific network routes or firewall rules
- generating or rotating source credentials
- starting or stopping DMS replication tasks
- Full Load and CDC sequencing
- retry and recovery decisions
- CDC checkpoint management
- replay and re-ingestion
- schema compatibility
- data-contract management
- ingestion-version allocation
- Raw History versioning and lifecycle
- validation processing
- metadata generation
- downstream Glue, Spark, MI or Analytical Platform processing

These responsibilities belong to the consuming service, the source database
owner or another component in the modular ingestion architecture.

## Relationship with DMS core

This module uses `dms-core` to create the underlying AWS DMS resources.

The source-ingestion module provides the stable, consumer-facing interface.
`dms-core` owns the lower-level DMS resources, IAM integration, endpoints,
replication tasks, logging and monitoring.

Consumers should normally use this module rather than calling `dms-core`
directly.

This separation allows the internal DMS implementation to evolve without
requiring every consuming service to manage the lower-level resource structure.

## Supported source engines

The following source engines are currently supported:

- PostgreSQL
- Oracle

The source engine is selected through `source_endpoint.engine`.

Engine-specific configuration remains optional and is isolated within the
relevant source configuration so that adding Oracle support does not alter
existing PostgreSQL behaviour.

## PostgreSQL sources

PostgreSQL-specific endpoint behaviour can be configured through the optional
`source_endpoint.postgres` object.

The supported settings include:

- boolean mapping
- failure behaviour for LOB truncation
- heartbeat enablement
- heartbeat frequency

When the source engine is PostgreSQL, the adapter supplies the existing
PostgreSQL defaults unless the consumer overrides them.

PostgreSQL settings cannot be supplied when the selected source engine is
Oracle.

## Oracle sources

Oracle source endpoints are configured through the same generic
`source_endpoint` interface used for PostgreSQL.

The consumer supplies:

- `oracle` as the source engine
- the Oracle database or service name expected by AWS DMS
- a Secrets Manager ARN containing the Oracle source credentials
- an optional KMS key ARN when the secret uses a customer-managed key
- the required SSL configuration
- optional DMS connection attributes
- optional Oracle ASM credential references

Oracle-specific DMS behaviour required for LogMiner or Binary Reader can be
supplied through `source_endpoint.extra_connection_attributes`.

The contents of `extra_connection_attributes` are passed to `dms-core` and then
to AWS DMS without being interpreted by this module.

The consuming service is responsible for selecting connection attributes that
are supported by:

- its Oracle version
- its AWS DMS engine version
- its source architecture
- its chosen CDC mechanism

This keeps the adapter reusable and prevents service-specific Oracle assumptions
from being embedded in the shared module.

## Oracle ASM credentials

The optional `source_endpoint.oracle` object supports Oracle ASM credential
references for Binary Reader configurations that require ASM access.

The consumer can supply:

- an ASM Secrets Manager ARN
- an optional customer-managed KMS key ARN for the ASM secret

The ASM configuration is optional.

Oracle sources that use LogMiner or Binary Reader configurations that do not
require separate ASM credentials do not need to supply the Oracle ASM
configuration.

The module passes the ASM secret reference to `dms-core`. Neither this module nor
`dms-core` reads or decodes the credential value.

## Secrets Manager

Source authentication is provided through AWS Secrets Manager.

The module accepts references to:

- the primary source database secret
- the optional KMS key used by the primary secret
- an optional existing DMS secret-access role
- the optional Oracle ASM secret
- the optional KMS key used by the ASM secret

Terraform passes these references to AWS DMS and does not retrieve or expose the
credential values.

If no source secret-access role is supplied, `dms-core` creates a
least-privilege role for the configured source secret and optional ASM secret.

If an existing role is supplied, the consuming service owns that role and is
responsible for granting it access to every required Secrets Manager secret and
KMS key.

For cross-account secrets, the infrastructure that owns the secret remains
responsible for the required Secrets Manager resource policy and KMS key policy.

## Oracle Full Load prerequisites

Before an Oracle Full Load task is started, the consuming service is responsible
for ensuring that:

- the DMS replication instance can reach the Oracle listener
- the source endpoint details are correct
- the configured secret contains valid Oracle credentials
- the DMS user can connect to the required database or service
- the DMS user can read the selected schemas and tables
- the supplied table mappings identify valid Oracle objects
- any required source security-group, routing and DNS configuration is present
- the source Oracle version is supported by the selected AWS DMS engine version

These source database requirements are not created by this module.

## Oracle CDC prerequisites

CDC requires additional preparation on the Oracle source.

Depending on the selected CDC mechanism and source architecture, the consuming
service is responsible for ensuring that:

- the database is configured for the required archive logging behaviour
- database-level supplemental logging is enabled
- the necessary table-level or schema-level supplemental logging is enabled
- archived redo logs are retained for an appropriate period
- the DMS user has the permissions required by the selected CDC mechanism
- LogMiner or Binary Reader prerequisites have been completed
- required Oracle parameters have been configured
- ASM credentials are available when the selected Binary Reader configuration
  requires ASM
- the required DMS connection attributes have been supplied
- the source configuration is compatible with the chosen AWS DMS engine version

The module exposes the required configuration points but does not modify the
Oracle source database.

The exact database preparation must be agreed with the source database owner and
validated against the AWS DMS documentation for the selected Oracle and DMS
versions.

## Network configuration

The module accepts the VPC and subnet configuration required by the DMS
replication instance.

Additional externally managed security groups can be attached to the replication
instance.

The module does not create source-specific inbound rules on the Oracle or
PostgreSQL database.

The consuming environment remains responsible for providing:

- routing between DMS and the source database
- DNS resolution
- source database ingress rules
- network ACL configuration where applicable
- connectivity to AWS services required by DMS
- connectivity to Secrets Manager and KMS where required

This allows each environment to apply its own network controls without embedding
them in the reusable module.

## S3 target

The module configures the S3 target endpoint used to land data replicated by
AWS DMS.

The consumer supplies:

- the target bucket
- an optional bucket folder
- an optional existing service-access role
- target encryption settings
- CDC batching and file-size settings

The adapter applies the shared platform ingestion conventions for the S3 output,
including Parquet output and operation metadata.

The target bucket itself remains outside this module.

The module does not manage downstream data lifecycle, Raw History, schema
versions or Glue publication.

## Replication tasks

The module accepts zero or more replication-task definitions.

Supported migration types are:

- `full-load`
- `cdc`
- `full-load-and-cdc`

The consumer supplies the table mappings and optional replication-task settings.

The module provisions the task definitions but does not decide when they should
run.

Runtime responsibilities remain outside Terraform, including:

- starting and stopping tasks
- Full Load and CDC sequencing
- choosing CDC start positions
- retries
- recovery
- replay
- re-ingestion

These responsibilities belong to the orchestration layer.

## Logging and monitoring

The module passes logging and monitoring configuration to `dms-core`.

CloudWatch task logging can be configured for the provisioned replication tasks.

Replication-instance alarms can be configured for:

- CPU utilization
- free storage space
- freeable memory

Notification action ARNs are supplied by the consumer. The module does not own
environment-specific SNS topics, Slack integrations or other notification
destinations.

## Environment portability

The module does not contain development, pre-production or production-specific
database configuration.

Each environment supplies its own:

- VPC
- subnet identifiers
- security groups
- source database name
- source Secrets Manager ARN
- KMS key ARNs
- source connection attributes
- S3 target
- replication-instance sizing
- task mappings
- logging settings
- monitoring settings
- tags

The same version of the source-ingestion module can therefore be deployed across
development, pre-production and production.

Temporary PostgreSQL or Oracle test databases are test infrastructure and are
not dependencies of this module.

## PostgreSQL compatibility

Oracle support is additive.

Existing PostgreSQL consumers continue to use the PostgreSQL-specific settings
and defaults exposed by the adapter.

Oracle settings are only passed to `dms-core` when the source engine is Oracle
and an ASM secret reference has been supplied.

PostgreSQL settings cannot be supplied for Oracle sources, and Oracle ASM
settings cannot be supplied for PostgreSQL sources.

This separation prevents Oracle configuration from changing existing PostgreSQL
endpoint behaviour.

## Terraform reference

The inputs, outputs, providers and resources below are generated from the
Terraform configuration using `terraform-docs`.

<!-- BEGIN_TF_DOCS -->
<!-- END_TF_DOCS -->
