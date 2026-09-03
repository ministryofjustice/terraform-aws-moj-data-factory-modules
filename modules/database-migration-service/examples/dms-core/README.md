# DMS Core Example

This example shows the basic interface for the reusable `dms-core` module.

It provisions the core AWS DMS infrastructure using network, source and target
resources supplied by the caller.

The example covers:

- a DMS replication instance
- a DMS replication subnet group created from supplied subnet IDs
- a dedicated replication-instance security group
- an Oracle or PostgreSQL source endpoint
- source authentication through AWS Secrets Manager
- an S3 target endpoint
- optional IAM role creation for source Secrets Manager and S3 target access
- configurable CloudWatch monitoring for the DMS replication instance

The example expects the caller to provide existing infrastructure such as:

- the VPC and subnets
- the source database secret
- the S3 target bucket
- KMS keys where required

Existing IAM role ARNs can optionally be supplied for source Secrets Manager
and S3 target access. If they are not supplied, the module creates the required
least-privilege roles.

Monitoring notification destinations are also supplied by the caller so the
module does not own SNS topics, Slack integrations or other notification
infrastructure.

The module does not read source credentials directly and does not create the
wider storage, orchestration, validation or processing layers.

Engine-specific DMS behaviour can be supplied through
`source_extra_connection_attributes` when required.

This example is not connected to an existing Data Factory or HMPPS deployment.
It is intended to demonstrate and validate the reusable module interface only.