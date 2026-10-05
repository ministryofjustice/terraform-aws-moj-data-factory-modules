# Schema registration

Deploys the container-based schema registration Lambda and its supporting
resources. The runtime is maintained separately under
`../../runtime/schema-registration/`.

## Responsibilities

This module creates:

- A contract registration DynamoDB table.
- The registration Lambda and execution role.
- Scoped S3, DynamoDB, logging and KMS permissions.
- An encrypted CloudWatch log group.
- An encrypted asynchronous failure queue.
- Operational alarms.

It does not generate or validate contracts, allocate versions, activate
contracts, provision DMS, create source databases or configure S3 notifications.

## Consumer prerequisites

The consumer supplies:

- A unique resource name.
- A published ECR image digest matching the selected architecture.
- Allowed S3 object ARNs or prefix patterns.
- Required encryption-key references and key policies.
- Alarm action ARNs if notifications are required.

The image repository must permit Lambda image retrieval. Cross-account image
and contract access also require policies in the resource-owning account.

The CloudWatch Logs key policy must permit the regional Logs service to use
the key for this log group.

No VPC attachment is required by this implementation.

## Contract table

Partition key: `contract_id` (String).

Sort key: `contract_version` (String).

The table uses on-demand capacity, customer-managed encryption, point-in-time
recovery and configurable deletion protection.

Each item represents one registered contract version, including its original
registration details. This is not a separate append-only audit-event table.

Versions such as `v2` and `v10` sort lexicographically. Consumers must not use
descending string order alone to identify the latest numeric version.

No TTL or indexes are created because the current registration interface
does not require them.

## Invocation

The handler currently accepts S3 notification-shaped payloads.

Only contracts permitted by upstream validation should reach registration.
The consumer owns trigger configuration and invocation permissions.

For synchronous invocations, the caller handles returned function errors.

For asynchronous invocations, Lambda retries according to configuration and
sends exhausted/expired invocations to the failure queue. The queue is not
automatically replayed.

An SQS event source would require a separate runtime adapter and consumer
configuration; the existing handler does not accept SQS event envelopes.

## Data protection

Registration records the S3 object version when available. Consumers must
use that reference when retrieving the registered content.

Bucket versioning and retention/protection are owned by the bucket component.
Registration conflict detection does not itself prevent S3 overwrites.

## Deployment

Build and publish the image before applying this module.

Changing `image_uri` to a new digest deploys the new image. Terraform does not
build images or start ingestion.

Reserved concurrency must fit the target account's available quota.

Deletion protection must be explicitly disabled before intentionally deleting
the table.

## Validation

Run Terraform formatting and validation, repository checks and runtime unit
tests before deployment.

Sandbox integration tests must verify registration, duplicate delivery,
content conflicts, object-version references, permissions, logs and
asynchronous failure handling.