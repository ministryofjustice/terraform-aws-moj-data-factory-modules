# DMS runtime controller

## What this is for

This controller gives our shared ingestion workflows a common way to
prepare a source, start an ingestion, check its progress, stop it and
resume it where that is safe.

The aim is to use the same code for PostgreSQL, Oracle and new services
we onboard later. Each service provides its configuration. We should
not need to copy the controller or add service-specific Python code.

This is the runtime work for ticket #323. Deploying it through reusable
Terraform and connecting it to the sandbox workflows comes under #324.

## What it does

The controller supports:

- Checking the source connection before starting or resuming.
- Synchronising credentials first, when the service needs it.
- Starting a new DMS task.
- Resuming a supported task from its existing checkpoint.
- Stopping a task.
- Checking task and table status.
- Checking whether the required tables have finished Full Load.
- Checking the recorded outcome of an earlier command.

It does not provision infrastructure, run Terraform, approve contracts
or create new ingestion versions.

## How it finds the right resources

A request supplies an existing ingestion ID rather than DMS resource
ARNs.

The controller uses that ingestion to look up its pinned configuration
and deployed resources. It checks that they belong to the environment,
account, region, source and database it is allowed to work with.

It also checks the published document versions and fingerprints against
the registry records.

This means a request cannot point the controller at a different task,
service or landing location.

Each deployment has an authorised service context, but all deployments
use the same Python implementation.

## Operations

| Operation | What it does |
| --- | --- |
| `preflight` | Prepare credentials where configured and check the source connection |
| `start` | Start a ready task that has never run |
| `resume` | Resume a supported stopped task using its existing checkpoint |
| `stop` | Request that a running task stops |
| `task_status` | Return the current task status |
| `table_status` | Return a page of table statistics |
| `full_load_status` | Check Full Load results for the required tables |
| `operation_status` | Check whether an earlier command has a recorded result or needs investigation |

Table recovery is not implemented. A `recover_table` request fails with
an explanation rather than attempting a reload.

## Sending a request

Every request needs a version, an action and an ingestion ID.

For example:

```json
{
  "request_version": 1,
  "action": "task_status",
  "ingestion_id": "existing-ingestion-id"
}
```

Requests must stay small. The limit is 16 KiB, and unknown fields are
rejected. Bulk configuration stays in the published configuration
documents.

### Preparing, starting, resuming and stopping

These operations also need an operation ID and the ownership claims
already obtained from the Ingestion Registry:

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

The ownership values above are examples. The caller must use the real
claims returned by the registry.

These claims let the controller check that the caller still owns the
ingestion and the shared task. The controller does not claim or renew
ownership itself.

### Checking table status

Table statistics are returned one page at a time. Pass the returned
cursor back as `marker` to request the next page.

### Checking Full Load

Full Load checks refer back to the original start operation:

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

If `selected_tables` is omitted, the controller checks all tables in the
pinned task group.

A supplied selection must contain unique tables from that group.

### Checking an earlier command

```json
{
  "request_version": 1,
  "action": "operation_status",
  "ingestion_id": "existing-ingestion-id",
  "target_operation_id": "start-one"
}
```

Status requests do not need command ownership claims, but they still
go through the authorised resource lookup.

## Starting is not the same as finishing

A successful start response means DMS accepted the command. It does not
mean the tables have finished loading.

The controller saves a start boundary before sending the initial start
command. Full Load checks use that original boundary so statistics from
an earlier load cannot be treated as results from this ingestion.

The check looks at each required table, including its state and load
timestamps. It returns `WAITING`, `BLOCKED`, `FAILED` or `SUCCEEDED`,
along with the individual table results.

The response also includes `task_requires_attention`. Callers should
check that separately because selected tables can finish loading while
the wider task still needs attention.

Full Load completion does not mean CDC has drained or that downstream
processing, history and publication have finished.

## Retrying commands safely

Start, resume and stop commands are recorded in the Ingestion Registry.

If the caller retries the same logical command, it must keep the same
operation ID. The controller returns the saved result rather than
sending the command again.

Reusing an operation ID for a different action or configuration is
rejected.

A saved result stays the result of that command. It is not updated to
reflect later task changes. Use a status operation for a fresh view.

This also applies to results such as `WAITING` or `IN_PROGRESS`. If the
workflow later decides another command is needed, it must make that
decision from fresh state and use a new operation ID.

Standalone preflight works differently: it runs a fresh check each time.
We do not reuse an old successful connection test after credentials may
have changed.

## Protecting shared tasks

Several table workflows may share one DMS task. They must not each
start, stop or resume it independently.

The controller checks both ingestion ownership and task-group
ownership. It also keeps a command sequence for the physical DMS task.

Before inspecting DMS, it takes a snapshot of that sequence. If another
command happens before it reserves its own command, the old observation
is rejected.

This prevents an older stop request from stopping a task that another
caller has since stopped and resumed.

An unresolved command also holds a gate against that physical task.
Changing the operation ID or taking over an expired lease does not
bypass it.

These protections apply to callers using this controller. Direct DMS
changes outside it are not coordinated by the registry.

The shared handler always uses persisted command control. The lower
level `TaskControl` still has an unpersisted mode for component
compatibility; that mode does not provide these durable protections.

## When we do not know whether a command succeeded

There is a gap between sending a command to DMS and saving its result.
A timeout or crash in that gap can leave us unsure whether DMS accepted
the command.

In that situation, the operation stays unresolved and further commands
against the task are blocked.

`operation_status` shows the registry reference and the current task
state so the problem can be investigated. It does not automatically
unlock the task.

Seeing a task running or stopped is not enough to prove which command
caused that state. We therefore do not guess, automatically resend the
command or clear the gate when a lease expires.

There is currently no automatic resolution or administrative reset API.
Resolving the situation needs verified evidence of the outcome and
confirmation that the original invocation can no longer send its command.

Registry records must not be deleted to work around this condition.

## What recovery is supported

| Task strategy | What is supported |
| --- | --- |
| Full Load only | Initial start, inspection and stop |
| Full Load and CDC | Initial start, inspection, stop and supported checkpoint-preserving resume |

For Full Load and CDC resume, the controller requires a previously
started, stopped task with 100% Full Load progress and no loading,
queued or errored tables.

Resume uses the existing DMS checkpoint. It does not supply a replacement
source position.

We currently reject:

- Full Load-only resume, because it can reload partial tables.
- Resume while Full Load is incomplete or has table errors.
- Automatic table reload or partial-table recovery.
- Initial CDC-only start without an agreed source-position strategy.

The current deployment schema supports Full Load and Full Load with CDC.

Recovery keeps the existing ingestion identity, version and landing
location. Re-ingestion is a separate operation which receives a new
identity/version and a safe extraction location through the registry.

Contract versions and ingestion versions are separate.

## PostgreSQL and Oracle credentials

Both engines use the same logical operations. Their source preparation
is configured differently.

For PostgreSQL, the existing setup invokes the credential-sync Lambda
before testing the DMS connection. The new deployment must explicitly
configure that behaviour.

For Oracle, the existing setup tests the connection without invoking
that credential-sync function.

Oracle rotation currently sends this event to its existing runtime
Lambda:

```json
{"action": "preflight"}
```

That existing path remains unchanged. It is not an ingestion-owned
request for this new handler.

When connecting the new deployment, we need to preserve the rotation
path without weakening the ownership checks used for ingestion commands.

## Polling and waiting

Source connection checks make at most 30 polling attempts, with
10 seconds between attempts.

Task and readiness operations return observations rather than waiting
indefinitely. The calling workflow is responsible for its wait interval,
retry limit and overall deadline.

Full Load checks inspect at most 10 statistics pages through the shared
handler. If that is not enough, they fail clearly rather than claim the
tables are ready.

The underlying readiness component supports a page limit up to 100.
Changing the shared handler's limit needs an explicit implementation
change.

Step Functions workflows and their deployment belong to #324. Any native
AWS integration used there must preserve resource authorisation and
shared-task coordination.

## Deployment settings

The Lambda handler is:

```text
dms_runtime.lambda_function.lambda_handler
```

These settings come from the deployment, not the request:

| Setting | What it supplies |
| --- | --- |
| `RUNTIME_ENVIRONMENT` | Environment the controller can work in |
| `RUNTIME_ACCOUNT_ID` | Authorised AWS account |
| `AWS_REGION` | AWS region, supplied by Lambda |
| `RUNTIME_SOURCE` | Stable source identity |
| `RUNTIME_DATABASE_NAME` | Database identity |
| `INGESTION_REGISTRY_TABLE_NAME` | Existing registry table |
| `PUBLISHED_DOCUMENT_BUCKET` | Configuration bucket |
| `PUBLISHED_DOCUMENT_PREFIX` | Allowed configuration prefix |
| `PUBLISHED_DOCUMENT_BUCKET_OWNER` | Expected bucket owner |
| `SOURCE_CREDENTIAL_MODE` | `connection_test` or `synchronise_then_test` |
| `CREDENTIAL_SYNC_FUNCTION_ARN` | Sync function, required only for `synchronise_then_test` |

For `connection_test`, omit `CREDENTIAL_SYNC_FUNCTION_ARN`.

The sync function must be in the authorised account and region. Its
reference is currently a deployment setting, not part of the version 1
published resource manifest.

## Building the Lambda package

With Docker running, use:

```bash
python modules/database-migration-service/runtime-controller/build_lambda.py \
  --architecture x86_64
```

The build runs inside the Python 3.12 Lambda Linux container. It packages
the runtime, configuration and registry libraries together with their
dependencies.

It also checks that the packages import and that the deployment schema
is included.

The ZIP is written to `dist/`. A `runtime-build.json` file inside it records
the resolved package versions, architecture and handler.

Rebuild after changing any of the three shared libraries.

The build uses hash-checked locks for its build tools and external runtime
dependencies and a pinned Python 3.12 Lambda container image.

The ZIP includes runtime-build.json with the installed package versions.
Its SHA-256 is printed after the build. Separate builds are not currently
guaranteed to produce byte-identical archives.

## Testing and current status

The 284 runtime tests have passed under Python 3.12/Linux and local
Python 3.14.

They use mocked AWS interactions. The package has also passed its Linux
import and schema checks.