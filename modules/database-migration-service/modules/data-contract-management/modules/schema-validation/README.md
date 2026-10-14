# Schema validation module

This module creates the infrastructure for checking incoming Parquet
schemas against registered Avro contracts.

The same module can be used for different services and environments.
The deployment supplies the image, source mappings, contract locations,
registry details, encryption keys and tags.

## What it creates

- A Lambda using a container image, with configurable memory, timeout
  and concurrency.
- An execution role with the permissions needed for validation.
- A separate DynamoDB table for validation results.
- An encrypted CloudWatch log group.
- An encrypted SQS queue for failed asynchronous invocations.
- Retry and event-age settings.
- Optional alarms for the Lambda and failure queue.

The ECR repository, source database, incoming bucket and contract registry
are managed outside this module.

The deployment also handles S3 notifications and permission for S3 to
invoke the Lambda.

## Source mappings

`source_mappings` tells validation which source and table an incoming
S3 file belongs to.

Each mapping contains the bucket, prefix and these identity fields:

- `source`
- `database_name`
- `schema_name`
- `table_name`

Each mapping identifies one table. Prefixes must end with `/`, must not
start with `/` and must not contain wildcards.

Mappings in the same bucket must not overlap. Different paths can map
to the same table, for example separate full-load and CDC paths.

These mappings are passed to the Lambda as JSON through `SOURCE_MAPPINGS`.
The module checks that the environment variables fit within Lambda's
size limit.

The mappings also define which incoming S3 paths the Lambda can read.

`ingestion_type` must be `FULL` or `CDC`. Both check each incoming file.

## Registry and validation results

The deployment supplies the existing registry table name and ARN.
They must refer to the same table.

Validation reads the registry but does not update it.

The module creates a separate table named `${name}-validation-audit` with:

- String partition key: `validation_id`.
- No sort key.
- On-demand billing.
- Point-in-time recovery.
- Customer-managed KMS encryption.
- Deletion protection enabled by default.

Each record holds the latest validation result for an incoming event.
Records are not automatically deleted.

## Permissions

The Lambda can:

- Query the configured contract registry.
- Write validation results to its audit table.
- Read incoming files under the configured prefixes.
- Read contracts under `contract_object_arns`.
- Read specific S3 object versions.
- Use the supplied KMS keys for these operations.
- Write logs and send failed invocation records to SQS.

The bucket and KMS key policies also need to allow this access,
including reads across accounts where needed.

Validation has no permission to read database secrets, write S3 files
or update the contract registry.

## Image and networking

`image_uri` must use a SHA-256 digest so the deployment uses a specific
image version.

The image must match `architecture`, which defaults to `arm64`.

The Lambda is not attached to a VPC. It reads from AWS services and
does not connect to the source database.

## Failure handling

Failed asynchronous invocations are retried according to
`maximum_retry_attempts`. If they still fail, the invocation record
is sent to the SQS failure queue. Expired events can also reach it.

The queue keeps failure records for 14 days.

It stores failures; it does not feed incoming files to the Lambda.
Failures from synchronous tests are not sent to this queue.

## Monitoring

Optional alarms check for:

- Lambda errors.
- Throttles.
- Dropped asynchronous events.
- Failed delivery to the failure queue.
- Failure records waiting in SQS.

Notification destinations are supplied through `alarm_action_arns`.
If the list is empty, the alarms are created without notifications.

## Outputs

The module returns:

- Lambda name, ARN and invoke ARN.
- Execution role ARN.
- Validation table name and ARN.
- Log group name.
- Failure queue ARN and URL.
- Alarm ARNs.

The deployment and later orchestration can use these outputs.