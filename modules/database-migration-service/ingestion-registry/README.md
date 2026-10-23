# Ingestion Registry

The Registry is the pipeline's memory. It keeps track of what's running,
what has finished and what failed, so we know where to continue without
starting everything again.

Each table has its own ingestion ID, version and progress. We also keep
the configuration, contract and data locations used for that ingestion.

This package reads and writes those records in DynamoDB. Deployment and
calling the actual pipeline components are handled separately.

## Ingestion versions

There are four different operations:

- **Initial ingestion:** ingest a table for the first time
- **Re-ingestion:** ingest it again with a new ingestion ID, version and
  separate data location
- **Recovery:** continue an existing ingestion after a failure, keeping
  its ID, version and data location
- **Replay:** process existing historical data again without extracting
  it from the source database

Contract versions and ingestion versions are separate. A new contract
doesn't automatically create a new ingestion version.

## Avoiding duplicate work

Each request has an ID and a fingerprint of its content.

Sending the same request again returns the original result. Sending
different content with the same request ID is rejected.

Request IDs belong to an environment and source. When requesting several
tables, each table needs its own request ID.

This also handles cases where a write succeeds but the response gets
lost. Retrying the request finds the result already recorded.

## Ownership and recovery

An execution takes ownership of an ingestion for a limited time and
renews that ownership while it works.

If it stops renewing, another execution can take over. Each takeover
gets a new attempt number and ownership token. Updates check these
values so an old execution can't overwrite the new owner's progress.

Before continuing after a takeover, the workflow still needs to check
what actually happened in the affected component. A missing response
doesn't always mean the operation failed.

These checks protect Registry updates. They don't stop a Lambda, DMS task
or processing job that's already running.

## Keeping configuration and data separate

Before extraction, we record the exact configuration, AWS resources and
landing location assigned to the ingestion. These stay fixed for that
ingestion.

Recovery uses those same details. Re-ingestion needs a separate safe data
location.

We keep a reservation for each table's landing location, even after the
ingestion finishes. This prevents another ingestion from reusing it and
mixing with the previous data.

The resource details must come from the shared configuration resolver.

## Contracts

We record the contract selected for the ingestion, including its version,
fingerprint, exact S3 object version and approval reference.

The Registry checks that the contract identity and path match the table.
Checking compatibility, approving a contract and activating it remain
part of the contract workflow.

An ingestion keeps the contract it started with.

## Tracking progress

Each stage records its own status and result reference. Successful results
are kept rather than overwritten. Failed or blocked stages include a
short explanation.

For the initial Full Load, we check that the contract, source checks,
extraction, processing, Raw History and publication have succeeded.
Reconciliation is also checked when required by the configuration.

Finishing that baseline doesn't mean the whole ingestion has finished.
CDC may still be bringing in changes.

`IngestionSummary.read()` provides the recorded overall status, stage
results, ownership state and recovery flag. Callers should use this
summary rather than treating the metadata status as overall progress.

## Finishing an ingestion

For Full Load only, completion needs a ready baseline and a reference to
evidence confirming that extraction has stopped.

For Full Load + CDC, we also need confirmation that:

- The source has stopped producing data
- Processing has handled the final input
- Raw History has saved the changes through that same point

Those results must refer to the same ingestion, DMS task and final input
boundary. Stopping DMS alone doesn't prove that everything downstream
has finished.

The components supplying these results are responsible for checking them.

Completion marks the ingestion as finished and allows a new ingestion
for that table. It keeps the historical records and landing reservations.

Retrying an old completion returns its original result without changing
a newer ingestion's reservation.

CDC-only completion is not supported yet.

## Tables sharing a DMS task

Some operations affect every table sharing a task. We track ownership
of the task separately from ownership of each table.

Finishing one table doesn't release the shared task. The workflow must
check the affected tables before starting, stopping or recovering it.

Before switching a task group to a new extraction generation, we retire
its previous scope. This requires every selected table to have released
its active ingestion reservation, matching coordinator tokens and a
reference to verified source-stop evidence.

Retirement keeps the old scope and physical task recorded permanently.
The next generation must use a new physical DMS task. Retrying retirement
doesn't change ownership of the new generation.

Atomic retirement currently supports up to 97 selected tables in a task
group. Larger groups are rejected without partial changes and need a
separate staged retirement implementation.

Retirement must happen before admitting the next generation's ingestions.

## Replay

Replay requests get their own ID and record:

- The original ingestion and version
- The exact historical input to read
- The processing revision to use
- The selected contract

The historical input must match the committed history recorded for the
original ingestion. Replay admission doesn't change that ingestion or
its data.

For now, this package records and checks replay requests. Running the
replay, tracking its execution, keeping its outputs separate and deciding
when to publish them are still to come.

## DynamoDB

The table needs two string keys:

- Partition key: `pk`
- Sort key: `sk`

It stores requests, ingestion versions, ownership, attempts, stage results,
resource details, data reservations, completion results and replay records.

Related changes are written together so a failed operation doesn't leave
half of the update behind.

There is no automatic record deletion or TTL configured here. Retention
must preserve the records needed for duplicate checks and recovery.

Table deployment, encryption, backups and permissions are handled through
the infrastructure work.

## Install and test

Run from this directory, with your virtual environment active:

```bash
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pip check

python -m pytest -c pytest.ini tests -q -p no:cacheprovider
```

The tests use Moto to simulate DynamoDB so no AWS credentials are needed.

They check duplicate requests, ownership, recovery attempts, data
reservations, progress, completion and replay admission. They don't prove
that the full pipeline works against live AWS components.

## Build

```bash
python -m pip wheel \
  --no-deps \
  --no-build-isolation \
  --wheel-dir dist \
  .
```

CI runs the tests on Python 3.12 and 3.14. It also installs the built
package in a separate environment and tests it outside the source
directory.