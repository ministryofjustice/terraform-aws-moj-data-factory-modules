# DMS runtime controller

## What this is for

This is the shared controller for our ingestion workflows. It handles
things like checking the source connection, starting a DMS task, checking
progress, stopping it and resuming it where that is safe.

The idea is to use the same implementation for PostgreSQL, Oracle and
the services we onboard later. Each service supplies its configuration.
We should not need to copy the controller or add Python code every time
we add another service.

This covers the runtime work for #323. The Terraform deployment and
connection to the sandbox workflows sit under #324.

## Contract orchestration

The package also includes the contract runtime built for #325. That
connects schema generation, validation, approval, registration and
activation. It also lets us pin the selected contract to an ingestion.

It has its own Lambda handler and request format, separate from the
DMS controller described here.

See [Contract runtime](docs/contract-runtime.md) for how that works,
how to configure it and what still needs to be connected.

## What the DMS controller does

It can:

- Check the source connection before starting or resuming.
- Synchronise credentials first where the service needs it.
- Start a new DMS task.
- Resume a supported task from its existing checkpoint.
- Stop a task.
- Check task and table status.
- Check whether the required tables have finished Full Load.
- Look up the recorded result of an earlier command.

The DMS controller does not run Terraform, create infrastructure,
approve contracts or allocate ingestion versions.

## How it knows which resources to use

A request supplies an existing ingestion ID. It does not supply DMS
resource ARNs.

The controller uses that ID to find the configuration and resources
already pinned to the ingestion. It checks the environment, account,
region, source and database before using them.

It also checks the exact published document versions and their
fingerprints against the Registry records.

That means a caller cannot use a request to point the controller at
another task, service or landing location.

Each deployment is configured for its allowed service context, but
the Python code stays shared.

## Operations

| Operation | What it does |
| --- | --- |
| `preflight` | Prepare credentials where needed and check the source connection |
| `start` | Start a ready task that has never run |
| `resume` | Resume a supported stopped task from its existing checkpoint |
| `stop` | Ask DMS to stop the task |
| `task_status` | Get the current task status |
| `table_status` | Get a page of table statistics |
| `full_load_status` | Check Full Load progress for the required tables |
| `operation_status` | Check the saved result of an earlier command or see whether it needs investigation |

Table recovery is not implemented. A `recover_table` request is rejected
with an explanation; it does not reload the table.

## Sending a request

Every request needs a version, an action and an ingestion ID:

```json
{
  "request_version": 1,
  "action": "task_status",
  "ingestion_id": "existing-ingestion-id"
}
```

Requests are limited to 16 KiB. Unknown fields are rejected.

The larger configuration stays in the published documents rather than
being passed around in every request.

### Preparing, starting, resuming and stopping

These operations also need an operation ID and the ownership claims
returned by the Ingestion Registry:

```json
{
  "request_version": 1,
  "action": "start",
  "ingestion_id": "existing-ingestion-id",
  "operation_id": "start-one",
  "ingestion_owner": {
    "execution_id": "table-execution-id",
    "fencing_token": 1,
    "attempt": 1
  },
  "task_owner": {
    "execution_id": "task-coordinator-id",
    "group_fencing_token": 1,
    "task_fencing_token": 1
  }
}
```

These ownership values are examples. Use the real claims returned by
the Registry.

The controller checks that the caller still owns both the ingestion
and the shared task. It does not obtain or renew those claims itself.

### Checking table status

Table statistics come back one page at a time. To get the next page,
pass the returned cursor back as `marker`.

### Checking Full Load

A Full Load check refers back to the operation that originally started
the task:

```json
{
  "request_version": 1,
  "action": "full_load_status",
  "ingestion_id": "existing-ingestion-id",
  "start_operation_id": "start-one",
  "selected_tables": [
    {
      "schema_name": "public",
      "table_name": "example"
    }
  ]
}
```

If `selected_tables` is omitted, all tables in the pinned task group
are checked.

If it is supplied, the tables must belong to that group and must not
contain duplicates.

### Checking an earlier command

```json
{
  "request_version": 1,
  "action": "operation_status",
  "ingestion_id": "existing-ingestion-id",
  "target_operation_id": "start-one"
}
```

Status requests do not need command ownership claims, but the resource
lookup still checks that the ingestion is within the allowed scope.

## A successful start does not mean the load has finished

A successful start response means DMS accepted the command. It does
not mean the tables are loaded.

Before sending the initial start command, the controller records the
start boundary. Later Full Load checks use that boundary so results
from an earlier load cannot be mistaken for this ingestion.

The check looks at each required table's state and load timestamps.
It returns `WAITING`, `BLOCKED`, `FAILED` or `SUCCEEDED`, along with
the table results.

Also check `task_requires_attention`. The selected tables can finish
while the wider task still has a problem.

Finishing Full Load does not mean CDC has drained or that downstream
processing, history and publication have finished.

## Retrying commands

Start, resume and stop commands are recorded in the Registry.

When retrying the same logical command, keep the same operation ID.
The controller uses the saved result instead of sending the command
again.

Using the same operation ID for different content is rejected.

The saved result describes that command. It does not change when the
task's state changes later. Use a status operation to get a fresh view.

That also applies to saved results such as `WAITING` or `IN_PROGRESS`.
If a fresh status check shows another command is needed, make that
decision explicitly and give the new command a new operation ID.

Standalone preflight is different. It runs a fresh connection check
each time because credentials or connectivity may have changed.

## When several tables share a task

Several table workflows can share one DMS task. They must not each
start, stop or resume it independently.

The controller checks ingestion ownership and task-group ownership.
It also keeps a command sequence for the physical DMS task.

Before inspecting DMS, it records the sequence it is looking at.
If another command changes that sequence before this command is
reserved, the older observation is rejected.

For example, an old stop request must not stop a task that another
caller has already stopped and resumed.

An unresolved command also blocks further commands against that task.
Changing the operation ID or taking over an expired lease does not
bypass that block.

These checks cover operations sent through this controller. Direct
changes in DMS are outside that coordination.

The shared handler always records command state. The lower-level
`TaskControl` also has an unpersisted mode for existing component use,
but that mode does not provide the same durable protections.

## When a command's outcome is unclear

A timeout or crash can happen after a command reaches DMS but before
we save its result.

When that happens, the command stays unresolved and further commands
against the task are blocked.

Use `operation_status` to see the Registry reference and current task
state. It helps with investigation, but it does not clear the block.

A task being running or stopped does not prove which command caused
that state. We therefore do not automatically resend the command or
clear the block when a lease expires.

There is no automatic resolution or administrative reset API yet.
Resolution needs evidence of what happened and confirmation that
the original invocation can no longer send its command.

Do not delete Registry records to work around this.

## Recovery we currently support

| Task strategy | Supported operations |
| --- | --- |
| Full Load only | Initial start, inspection and stop |
| Full Load and CDC | Initial start, inspection, stop and supported resume from the existing checkpoint |

For Full Load and CDC, resume requires a task that:

- Has started before.
- Is currently stopped.
- Has reached 100% Full Load.
- Has no loading, queued or errored tables.

Resume keeps the existing DMS checkpoint. It does not replace the
source position.

We currently reject:

- Full Load-only resume, because it could reload partially loaded tables.
- Resume before Full Load finishes or while table errors remain.
- Automatic table reload and partial-table recovery.
- Initial CDC-only start without an agreed source-position strategy.

The deployment schema currently supports Full Load and Full Load with CDC.

Recovery keeps the ingestion identity, version and landing location.
Re-ingestion is separate: it gets a new identity/version and a safe
extraction location through the Registry.

Contract versions and ingestion versions are separate too.

## PostgreSQL and Oracle credentials

The logical operations are shared, but source preparation differs.

For PostgreSQL, the existing setup calls the credential-sync Lambda
before testing the DMS connection. The new deployment must explicitly
configure that behavior.

For Oracle, the existing setup tests the connection without calling
that sync function.

Oracle rotation currently sends this to its existing runtime Lambda:

```json
{"action": "preflight"}
```

That path stays unchanged. It is not an ingestion-owned request for
the new handler.

When connecting the deployment, we need to keep rotation working
without weakening the ownership checks on ingestion commands.

## Polling and waiting

Source connection checks make up to 30 attempts, waiting 10 seconds
between attempts.

Task and readiness checks return what they observe. The calling
workflow decides when to check again, how many times to retry and
when to stop waiting.

The shared Full Load handler checks at most 10 pages of table
statistics. If that is not enough, it fails clearly rather than
claiming the tables are ready.

The underlying component supports up to 100 pages, but changing
the shared handler's limit requires a code change.

Step Functions workflows and deployment sit under #324. Any native
AWS integrations added there must keep the same resource-scope
and shared-task protections.

## Deployment settings

The DMS Lambda handler is:

```text
dms_runtime.lambda_function.lambda_handler
```

These settings come from deployment configuration, not operation requests:

| Setting | What it supplies |
| --- | --- |
| `RUNTIME_ENVIRONMENT` | Allowed environment |
| `RUNTIME_ACCOUNT_ID` | Allowed AWS account |
| `AWS_REGION` | Region supplied by Lambda |
| `RUNTIME_SOURCE` | Stable source identity |
| `RUNTIME_DATABASE_NAME` | Database identity |
| `INGESTION_REGISTRY_TABLE_NAME` | Existing Ingestion Registry table |
| `PUBLISHED_DOCUMENT_BUCKET` | Published configuration bucket |
| `PUBLISHED_DOCUMENT_PREFIX` | Allowed configuration prefix |
| `PUBLISHED_DOCUMENT_BUCKET_OWNER` | Expected bucket owner |
| `SOURCE_CREDENTIAL_MODE` | `connection_test` or `synchronise_then_test` |
| `CREDENTIAL_SYNC_FUNCTION_ARN` | Required only for `synchronise_then_test` |

For `connection_test`, leave out `CREDENTIAL_SYNC_FUNCTION_ARN`.

The sync function must be in the allowed account and region.
Its ARN is currently a deployment setting, not part of the version 1
published resource manifest.

## Building the Lambda package

With Docker running, run this from the repository root:

```bash
python modules/database-migration-service/runtime-controller/build_lambda.py \
  --architecture x86_64
```

The build runs inside the Python 3.12 Lambda Linux container. It packages
the runtime, configuration and Registry libraries with their dependencies.

It checks that both runtime entry points import and that the deployment
schema is included.

The ZIP is written to `runtime-controller/dist/`. The
`runtime-build.json` inside it records package versions, architecture
and the default DMS handler.

The contract handler is also packaged, but must be selected separately
in its Lambda deployment.

Rebuild the package after changing any of the three shared libraries.

The build uses hash-checked dependency locks and a pinned Lambda
container image. It prints the ZIP's SHA-256 after building.

Separate builds are not currently guaranteed to produce byte-identical
archives.

## Testing and current status

CI runs the runtime tests under Python 3.12 and 3.14. It also tests the
installed package wheels outside the source tree.

Tests use mocked AWS clients, in-memory versioned document stores and
Moto-backed services. Contract integration tests call the actual
generation, validation and registration handlers with simulated database
metadata.

The Linux package build checks imports, packaged dependencies and the
deployment schema.

These checks do not prove live AWS permissions, database connectivity
or successful deployed ingestion. Those still need to be checked in AWS.

## Publishing the development package

After a push to `integration/data-hub-modularisation`, CI waits for
the Python and Terraform checks before publishing the tested ZIP.

The publishing job takes the package from the same workflow run.
It checks the checksum, source commit and build details before
authenticating to AWS.

Packages are stored under:

```text
lambda/dms-runtime-controller/<source-commit>/<zip-sha256>/
```

The job uploads the ZIP, build metadata and checksum file without
overwriting existing files. If a file already exists, its contents
and encryption must match.

It then reads the exact S3 object versions back and checks their
contents.

The resulting `deployment-reference.json` records the bucket, object
keys, versions, checksums and source details. It is kept as a workflow
artifact for the later Terraform deployment.

The reference contains AWS resource identifiers, so it is not printed
in the workflow summary.

Publishing the package does not deploy a Lambda or start ingestion.

The workflow uses:

- `DMS_RUNTIME_ARTIFACT_BUCKET`
- `DMS_RUNTIME_ARTIFACT_KMS_KEY_ARN`
- `DMS_RUNTIME_ARTIFACT_PUBLISHER_ROLE_ARN`

Pull requests and manual validation runs do not publish packages.