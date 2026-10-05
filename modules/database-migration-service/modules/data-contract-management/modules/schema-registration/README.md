# Schema registration

This module deploys the schema registration Lambda and the AWS resources it
needs. The Lambda code lives in `../../runtime/schema-registration/`.

## What it creates

- A DynamoDB table for registered contract versions.
- The registration Lambda and its execution role.
- Permissions to read the configured S3 objects, write registration records,
  use the required encryption keys and write logs.
- An encrypted CloudWatch log group.
- An encrypted queue for failed asynchronous invocations.
- Alarms to help us spot failures and throttling.

Schema generation and validation are handled by their own components.
Registration records the permitted contract version; it does not allocate
versions or activate contracts.

DMS, source databases and S3 triggers are configured separately.

## What you need to provide

When calling this module, provide:

- A unique name for the resources.
- A published ECR image URI pinned to a digest.
- The architecture matching that image.
- The S3 objects or prefixes the Lambda is allowed to read.
- The required encryption-key references.
- Alarm action ARNs if notifications are needed.

The ECR repository must allow Lambda to retrieve the image. If images or
contracts are in another account, that account must also allow access.

The CloudWatch Logs encryption key must allow the regional Logs service to
use it for this log group.

This Lambda does not need a VPC connection.

## DynamoDB table

The table uses:

- `contract_id` as the partition key.
- `contract_version` as the sort key.

It has on-demand capacity, customer-managed encryption, point-in-time
recovery and configurable deletion protection.

Each record represents one registered contract version and keeps its original
registration details. It does not create a separate audit row for every
validation event or duplicate registration attempt.

Contract versions are stored as strings, so their order is not numeric.
For example, `v10` sorts before `v2`. Do not rely on string sorting to find
the latest version.

There is no TTL or secondary index because the current registration code
does not need them.

## Calling the Lambda

The handler expects an S3 notification-shaped payload.

Contracts must have passed upstream validation before reaching registration.
The deployment calling this module is responsible for connecting the correct
trigger and setting invocation permissions.

For synchronous calls, the caller must check and handle Lambda errors.

For asynchronous calls, Lambda retries using the configured limits. Failed
or expired invocations are sent to the failure queue for investigation.
Messages are not automatically replayed.

The handler does not currently accept SQS events. Connecting an SQS trigger
would need an adapter and the corresponding deployment configuration.

## Protecting contract content

When available, registration stores the S3 object version alongside the URI.
Use that version reference when reading the registered contract.

The component managing the bucket is responsible for versioning and content
protection. Rejecting a conflicting DynamoDB registration does not prevent
someone overwriting an S3 object.

## Deployment

Build and push the image before applying this module.

Update `image_uri` to the new image digest when deploying a new build.
Terraform deploys the resources; it does not build images or start ingestion.

Check that the account has enough available concurrency for the configured
Lambda reservation.

To intentionally delete the table, first disable its deletion protection
through Terraform.

## Checks and testing

Before deployment, run:

- Terraform formatting and validation.
- The required repository checks.
- The registration Lambda unit tests.

After deploying in sandbox, test new registration, duplicate events,
conflicting content, S3 object-version references, permissions, logging and
asynchronous failure handling.