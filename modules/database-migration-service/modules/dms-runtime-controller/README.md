# DMS runtime controller

This module deploys the DMS runtime controller for a service.

The controller gives our workflows a common way to prepare a source,
start or stop a DMS task and check how an ingestion is progressing.
We use the same code and Terraform module for PostgreSQL, Oracle and
other services we add later.

Each controller is configured for one service. The Registry and Lambda
package can be shared across services.

## What this module creates

- A Lambda function
- An execution role for the Lambda
- A CloudWatch log group

The module doesn't create DMS tasks or start an ingestion. Those are
handled separately.

## What it needs

To deploy a controller, the calling configuration needs to provide:

- The service and environment the controller can work with
- The published Lambda ZIP, including its S3 object version and checksum
- The shared Ingestion Registry table
- The location of the service's published configuration
- The DMS tasks, replication instances and source endpoints it can use
- How the source credentials should be prepared

The Lambda package must be built for Python 3.12 and x86_64.

The S3 object version identifies the package to deploy. The checksum helps
Terraform track code changes, but the publishing process must also verify
that the uploaded ZIP matches it.

## PostgreSQL and Oracle credentials

Both engines use the same controller operations. The difference is how
the source is prepared:

- **PostgreSQL:** use `synchronise_then_test` and provide the credential
  sync Lambda ARN.
- **Oracle:** use `connection_test` without a sync Lambda ARN.

The existing Oracle rotation and preflight path remains in place. It is
separate from this controller.

## Access

The controller checks that an ingestion belongs to its configured service.
Its role is also limited to the configured DMS tasks and published
configuration prefix.

The Registry is shared. At the moment, the role's DynamoDB permissions
cover the shared table; they are not separated by service in IAM. We need
to review this before deploying the controller.

Some DMS describe operations require access to all resources. Those
permissions are restricted to the configured region.

Table reload and recovery are not supported by this module.

## Testing

From this module's directory:

```bash
terraform init -backend=false -input=false
terraform validate
terraform test
```

The tests check the module configuration and IAM policy without creating
AWS resources. We still need to test the deployed controller and its
permissions in the sandbox.