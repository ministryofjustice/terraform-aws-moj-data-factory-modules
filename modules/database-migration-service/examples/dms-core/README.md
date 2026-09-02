# DMS Core Example

This example shows the basic interface for the reusable `dms-core` module.

It provisions the core AWS DMS infrastructure using network, source and target
resources supplied by the caller.

The example covers:

- a DMS replication instance
- a DMS replication subnet group created from supplied subnet IDs
- a dedicated replication-instance security group
- an Oracle or PostgreSQL source endpoint
- source authentication through AWS Secrets Manager references
- an S3 target endpoint

The example expects the caller to provide existing infrastructure such as:

- the VPC and subnets
- the source database secret
- the IAM role used by DMS to access that secret
- the S3 target bucket
- the IAM role used by DMS to access the target bucket
- KMS keys where required

The module does not read source credentials directly and does not create the
wider storage, IAM, orchestration, validation or processing layers.

Engine-specific DMS behaviour can be supplied through
`source_extra_connection_attributes` when required.

This example is not connected to an existing Data Factory or HMPPS deployment.
It is intended to demonstrate and validate the reusable module interface only.