# Contract runtime

## What this is for

This connects our schema generation, validation and registration
capabilities to the ingestion process.

The coordinator calls those capabilities and acts on their results.
It does not contain another implementation of schema generation
or compatibility checking.

So far, we have built:

- Lookup using the published deployment documents.
- Contract version allocation.
- Candidate generation and recovery.
- Checks on the validation results and their stored evidence.
- Human approval checks and explicit policy-based acceptance.
- Promotion, confirmed registration and activation.
- Current-contract lookup and ingestion contract pinning.
- Request handling and a separate Lambda bootstrap.

These are covered by local and CI tests. Deployment wiring and live
AWS verification are still outstanding.

The authenticated approval interface, Slack notifications and review
links have not been implemented yet.

## Which handler to use

The contract Lambda handler is:

```text
dms_runtime.contract_lambda_function.lambda_handler
```

The existing DMS handler remains:

```text
dms_runtime.lambda_function.lambda_handler
```

Both are included in the shared runtime package. The Lambda deployment
must explicitly select the handler it needs.

Publishing the package does not deploy either function.

The contract bootstrap is configured for one selected table. A request
cannot change that table or supply another deployment revision, Lambda
ARN, storage location, policy or reviewer identity.

## How the process works

We first check whether an approved contract is already available.
That lookup does not automatically generate another candidate.

When generation is needed:

1. Preparation allocates a contract version and records the deployment
   context.
2. Generation writes a versioned candidate.
3. The assessment request records the exact candidate, approved baseline
   or absence of a baseline, and assessor revision.
4. Validation checks the contract and writes its assessment evidence.
5. Admission independently checks that evidence against the request,
   candidate, baseline and assessor revision.
6. The lifecycle blocks the request, reuses the baseline, asks for review
   or accepts a compatible change under an explicitly enabled policy.
7. An authorised candidate is promoted and registration is confirmed.
8. Activation makes it the current contract for future lookup.

Registration and activation can finish before an ingestion exists.

Uploading a candidate does not make it approved or ready for ingestion.

## Deployment configuration

The bootstrap reads `CONTRACT_RUNTIME_CONFIG` as JSON from the Lambda
environment. `AWS_REGION` must match the region in the configuration.

This is deployment configuration. It must not come from an ordinary
operation request.

The example below shows the expected structure. Replace every placeholder
with the actual deployment values before using it.

```json
{
  "configuration_version": 1,
  "registry_table_name": "development-ingestion-registry",
  "lookup": {
    "environment": "development",
    "account_id": "123456789012",
    "region": "eu-west-2",
    "source": "sandbox-postgres",
    "database_name": "dms_test",
    "schema_name": "public",
    "table_name": "example",
    "configuration_revision": "<configuration-sha256>",
    "artifact_revision": "<artifact-sha256>"
  },
  "documents": {
    "bucket_name": "example-platform-configuration",
    "prefix": "services/sandbox-postgres/",
    "bucket_owner_account_id": "123456789012",
    "manifest": {
      "bucket_name": "example-platform-configuration",
      "object_key": "services/sandbox-postgres/manifest/<sha256>.json",
      "version_id": "<manifest-s3-version>",
      "sha256": "<manifest-bytes-sha256>"
    },
    "artifact": {
      "bucket_name": "example-platform-configuration",
      "object_key": "services/sandbox-postgres/artifact/<sha256>.json",
      "version_id": "<artifact-s3-version>",
      "sha256": "<artifact-bytes-sha256>"
    },
    "catalog": {
      "bucket_name": "example-platform-configuration",
      "object_key": "services/sandbox-postgres/catalog/<sha256>.json",
      "version_id": "<catalog-s3-version>",
      "sha256": "<catalog-bytes-sha256>"
    }
  },
  "settings": {
    "candidate": {
      "bucket_name": "example-contract-candidates",
      "prefix": "candidates/",
      "bucket_owner_account_id": "123456789012"
    },
    "approval": {
      "bucket_name": "example-contract-evidence",
      "prefix": "approvals/",
      "bucket_owner_account_id": "123456789012"
    },
    "evidence": {
      "bucket_name": "example-contract-evidence",
      "prefix": "validation/",
      "bucket_owner_account_id": "123456789012"
    },
    "approved_bucket_name": "example-approved-contracts",
    "approved_bucket_owner_account_id": "123456789012",
    "candidate_kms_key_arn": "arn:aws:kms:eu-west-2:123456789012:key/<candidate-key-id>",
    "approved_kms_key_arn": "arn:aws:kms:eu-west-2:123456789012:key/<approved-key-id>",
    "assessor_revision": "<assessor-release-revision>",
    "policy": {
      "revision": "<policy-revision>",
      "automatically_accept_compatible": false
    }
  },
  "authorised_reviewers": [
    "<trusted-reviewer-subject>"
  ]
}
```

SHA-256 values must contain 64 lowercase hexadecimal characters.
S3 references must contain actual object version IDs. The null version
is rejected.

The published catalog supplies the selected table's generation and
validation functions, the registration function and the contract
registry. The factory builds the services from those references.

Invoked Lambda references must use published numeric versions.

The assessor revision must match the validation Lambda configuration.
When the assessment rules change, change that revision too. Do not
reuse a revision for rules that could give a different result for
the same request.

The handler is cached while the Lambda execution environment stays warm.
Its deployment configuration is not replaced by each operation request.

The JSON parser allows up to 16 KiB, but that is not a Lambda
environment-variable allowance. Minify the JSON and check the total
environment-variable size against the AWS deployment limit.

This bootstrap does not currently load its configuration from an
external configuration file.

Do not put database credentials or other secrets in this JSON.

## Sending requests

Every request needs the integer `request_version: 1`.

Unknown fields and unsupported operations are rejected. The request
limit is 16 KiB.

### Find the current approved contract

```json
{
  "request_version": 1,
  "operation": "RESOLVE_CONTRACT"
}
```

If no current approved contract exists, this returns `CONTRACT_REQUIRED`.

Otherwise it returns `CONTRACT_READY`, with action `RESOLVED` and
the checked contract selection.

A verification failure raises an error. It does not mean there is
simply no contract.

### Prepare a candidate

```json
{
  "request_version": 1,
  "operation": "PREPARE_CONTRACT",
  "request_id": "example-contract-request"
}
```

This returns `CANDIDATE_READY`, including:

- `request_id`
- `reservation_key`
- `preparation_fingerprint`
- `contract_version`
- `candidate_reference`
- `contract_fingerprint`
- `generation_context`

Keep these values for assessment and recovery.

A request ID must contain 1–256 UTF-8 bytes, without surrounding
whitespace.

When retrying the same request, keep the same ID and deployment context.
Using the same ID for conflicting content is rejected.

This allocates a contract version, not an ingestion version.

### Assess the candidate

```json
{
  "request_version": 1,
  "operation": "ASSESS_CONTRACT",
  "reservation_key": "CONTRACTVERSIONREQUEST#<64-character-digest>",
  "preparation_fingerprint": "<fingerprint-from-preparation>"
}
```

Use the reservation key and preparation fingerprint returned by
`PREPARE_CONTRACT`.

This records or reuses the assessment request, calls the published
validation function and checks which Lambda version ran.

The returned evidence reference is then independently checked before
the lifecycle acts on it.

| Status | What it means |
| --- | --- |
| `REVIEW_REQUIRED` | Someone needs to review the contract before it can continue |
| `BLOCKED` | The assessment or recorded rejection prevents it from continuing |
| `CONTRACT_READY` | We reused the approved baseline or activated an authorised contract |

Invocation failures, invalid evidence and conflicts raise errors.
They must not be treated as approval or readiness.

### Resume after a decision

```json
{
  "request_version": 1,
  "operation": "RESUME_CONTRACT",
  "request_id": "example-contract-request",
  "contract_version": "v3",
  "expected_request_fingerprint": "<lifecycle-request-fingerprint>"
}
```

Use the contract version from preparation and the lifecycle request
fingerprint returned with the review-required result.

The lifecycle request fingerprint and preparation fingerprint are
different values. Do not swap them.

Resume checks the recorded assessment and current decision again.
If review is still pending, it stays `REVIEW_REQUIRED`. A rejected
request stays blocked.

An authorised request can continue through registration and activation.

Calling resume does not approve the request or submit a review.

## What happens for each assessment result

| Result | Default behavior |
| --- | --- |
| Invalid contract | Block |
| Unsupported validation or comparison | Block |
| First contract | Ask for human approval |
| No change | Reuse the approved baseline |
| Compatible change | Ask for human approval |
| Breaking change | Ask for human approval |

Compatible changes can be accepted under policy only when
`automatically_accept_compatible` is explicitly enabled and the
recorded policy matches the runtime's trusted policy.

That option is off by default. Having the code available does not
mean the team has agreed to enable it.

The compatibility rules currently cover a defined set of cases.
They are not a complete general-purpose Avro compatibility checker.

For example:

- An otherwise unchanged supported contract can reuse the baseline,
  even though preparation allocated a new contract version.
- Appended nullable fields with explicit null defaults can be compatible.
- Required additions without defaults and removed fields can be breaking.
- Numeric type changes without an implemented rule are unsupported.
- Metadata, logical-type or complex-type changes that we cannot classify
  are blocked rather than guessed compatible.

The validation runtime's rules and tests define the supported cases.
A different hash alone does not tell us that a change is breaking.

Also, `ASSESSMENT_COMPLETED` only means the validation function finished
and produced evidence. That evidence can still say `INVALID` or
`UNSUPPORTED`.

## How approval works

The ordinary contract handler does not accept approval requests.

The reviewer allowlist checks whether the reviewer named in stored
approval evidence is allowed to approve the configured table.
An empty list allows nobody.

That check does not establish who the person is. It does not authenticate
a Slack user or prove who submitted the approval.

The approval integration still needs to authenticate the reviewer,
check their scope and record the decision through the lifecycle
interface. Its evidence must be stored where only authorised writers
can create it.

The approval checks tie the decision to the exact request fingerprint,
assessment, policy and versioned evidence. Resume checks the current
authorisation again before continuing.

The authenticated submission flow, Slack notification and review link
are still outstanding.

## Versions, registration and activation

Orchestration owns contract version allocation. Generation receives
a reserved version, and registration does not allocate another one.

Deployment needs to make sure another writer is not independently
allocating versions for the same contract. The allocator checks
existing registered versions when establishing its sequence.

We use these metadata names throughout the process:

- `source`
- `database_name`
- `schema_name`
- `table_name`
- `contract_version`

Registered contracts are stored at:

```text
source/database_name/schema_name/table_name/contract_version/contract.avsc
```

Before activation, we confirm registration against the actual object
version, URI and full-contract fingerprint.

The `CONTRACT_CURRENT` record tells this runtime which contract to
select for future use. Registering a contract does not update that
selection by itself.

The registration record's `is_active` field is not what this runtime
uses to decide which contract is current.

## Pinning a contract to ingestion

`CurrentContractPinning` takes an existing ingestion ownership claim.
It records the selected registered contract and its authorisation
in that ingestion's `CONTRACT` stage.

Pinning is available through the library. It is not one of the four
contract Lambda operations above.

Once an ingestion has a completed pin, a later contract activation
does not replace it. Retrying that pin returns the original selection,
but still checks that the caller owns the ingestion.

Adopting a contract does not start DMS, trigger Full Load or backfill
historical data. Those remain separate decisions in the ingestion
workflow.

## Fingerprints and retries

We use different fingerprints for different checks:

| Value | What we use it for |
| --- | --- |
| Raw object SHA-256 | Check the exact bytes of a versioned object |
| Full-contract fingerprint | Identify the normalised complete contract |
| Structural schema fingerprint | Compare the canonical field structure |

The legacy registration field called `schema_fingerprint` currently
holds the full-contract fingerprint. Do not treat it as a hash of
the field structure alone.

Candidate, promotion and evidence writes check whether an object
already exists and whether its contents match.

If the same version points to different content, the operation is
blocked.

After a timeout, keep the same request identifiers and pinned references
so recovery can check what already happened. Do not allocate a new
version just because an invocation response was lost.

A retry can invoke a capability again. The protection is against
duplicate recorded progression and conflicting content, not a promise
that a Lambda runs only once.

## What deployment needs

The deployment needs:

- The shared package with the contract handler selected.
- The actual manifest, artifact and catalog references.
- Versioned storage for candidates, approved contracts and evidence.
- Matching storage configuration across the capabilities and runtime.
- Matching assessor revisions and published numeric Lambda versions.
- The trusted policy and reviewer configuration.
- Suitable Lambda timeouts and workflow retry limits.
- Restricted write access for assessment and approval evidence.

The orchestration Lambda's role needs permission to:

- Read the selected published document versions.
- Read and conditionally update the relevant Ingestion Registry records.
- Query and read the selected contract registry.
- Read candidate, approval, validation and registered-contract versions.
- Write and verify promoted contract objects.
- Invoke the selected published Lambda versions.
- Use the required KMS keys and write logs.

Generation, validation and registration use their own execution roles.
Their source, storage and Registry permissions also need to be checked.

The deployment workflow's role is separate from those runtime roles.
Read-only OIDC access does not allow a workflow to apply infrastructure
changes, update Terraform state or write publication records.

We still need to agree the MP deployment route, Terraform state ownership
and any handover. We must not assume a workflow exception has been
approved or let competing pipelines manage the same Terraform resources.

## What has been tested and what is left

CI covers source tests, installed-wheel tests and package imports.

Integration tests call the actual capability handlers using simulated
AWS services and database metadata. That checks the code paths, but
does not prove live permissions, network access, database behavior
or successful deployed ingestion.

Before calling the deployed integration complete, we still need to check:

- The agreed deployment route and one clear Terraform owner.
- The published package and configuration references.
- Lambda invocation, storage, Registry and KMS permissions in AWS.
- Source connectivity and generation against the selected database.
- Assessment, approval, registration and activation in AWS.
- Ingestion pinning, including keeping an existing pin after activation.
- Recovery after failures and rejection of conflicting content.
- The authenticated approval and notification experience.

Candidate assessment is separate from the existing Parquet-file
validation path.

These contract tests do not establish end-to-end DMS, CDC, historical
processing or publication behavior.