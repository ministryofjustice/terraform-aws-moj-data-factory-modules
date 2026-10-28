# Service ingestion configuration

This tool takes a service YAML file, checks it and generates the inputs
needed by the reusable ingestion modules.

PostgreSQL and Oracle use the same format. A service lists the tables it
needs, how they are grouped into DMS tasks and which settings to use.

The aim is to add services through configuration without copying Python,
Terraform or workflow code for each one.

For now, this supports development in `eu-west-2`.

## What it does

The tool:

- Checks the service YAML and platform settings
- Rejects missing fields, unknown settings and duplicate tables
- Resolves the service's secret and configuration profiles
- Generates DMS mappings for exactly the selected tables
- Creates stable deployment and task names
- Generates module inputs and runtime configuration
- Calculates revisions so the configuration used can be traced

It runs locally and does not need AWS credentials.

It does not deploy resources, connect to the database, generate contracts
or start ingestion. Those steps are handled by the deployment and
orchestration components.

## Install

Use Python 3.12 or newer.

From the repository root:

```bash
python3 -m venv /tmp/ingestion-config-venv
source /tmp/ingestion-config-venv/bin/activate

python -m pip install \
  -e ./modules/database-migration-service/configuration \
  -r modules/database-migration-service/configuration/requirements-dev.txt
```

Check the commands:

```bash
ingestion-config --help
```

## Configuration files

The tool uses two files:

| File | What it contains |
| --- | --- |
| Service YAML | The source, selected tables, task groups and requested settings |
| Platform bindings | The environment settings and profiles the service is allowed to use |

Examples are available in:

- `examples/postgres.yaml`
- `examples/oracle.yaml`
- `examples/platform-development.yaml`

The platform example uses dummy AWS identifiers. Real deployments need
the correct environment values.

Platform bindings are managed by the platform. Passing validation does
not confirm that AWS permissions, bucket policies or KMS access are
configured correctly.

## Service YAML

The service YAML contains:

- A stable service name
- Database engine and database name
- A secret reference
- Schemas and tables to ingest
- DMS task groups and ingestion mode
- Contract settings
- Processing, history and publication profiles
- Destination and concurrency profiles
- Scheduling settings

Credentials stay in Secrets Manager. Do not put usernames or passwords
in the YAML.

Execution IDs, attempts, ingestion versions and progress are created at
runtime and do not belong in this file.

Database, schema and table names keep their original case.

The first version supports identifiers matching:

```text
[A-Za-z_][A-Za-z0-9_$#]{0,127}
```

Names outside this format are rejected rather than changed automatically.

## Tables and task groups

The supported modes are:

- `full-load`
- `full-load-and-cdc`

Each table belongs to one task group within a service. The same schema
and table cannot appear twice, including in different groups.

The ingestion mode is set for the whole task group.

Tables must be listed explicitly. Wildcards are not supported.

The generated DMS rules use `rule-action: explicit`, so names containing
underscores are treated as exact names.

If five tables are selected, the compiler generates five selection rules.
It does not look up the database and add other tables.

Changing the order of tables in the YAML does not change the output.

## Contracts

Use this when a contract should be generated if an eligible one does
not already exist:

```yaml
contract:
  strategy: generate_if_missing
  namespace: uk.gov.justice.contracts
```

Use this when the service needs a specific existing contract version:

```yaml
contract:
  strategy: existing
  contract_version: v1
```

The source, database, schema and table identify the contract. The YAML
`service` value is used as the runtime `source`.

The compiler records what is required. It does not check the registry,
allocate a contract version or generate the Avro file.

Orchestration will resolve the contract and keep its exact reference
with the ingestion before the affected extraction starts.

## Platform settings

Platform bindings contain the available:

- Service and secret bindings
- Network profiles
- Replication-instance profiles
- Destination profiles
- Concurrency profiles
- Processing, history and publication profile names

The resolver checks that referenced profiles exist and that the service
is allowed to use its selected destination and concurrency settings.

The engine, database name and secret reference must match the service's
platform binding. The parent domain must also match the destination.

AWS identifier syntax and Region are checked locally. Checking that the
resources exist and can be accessed happens during deployment or
preflight.

Cross-account KMS keys can be supplied through platform bindings.
Their permissions still need to be configured separately.

A replication profile currently supplies instance settings. It does not
mean that services automatically share a replication instance.

A processing, history or publication profile being listed does not mean
that the component is deployed or ready to run.

## Scheduling

To leave scheduling disabled:

```yaml
schedule:
  enabled: false
```

The first version supports recurring full-load re-ingestion:

```yaml
schedule:
  enabled: true
  expression: rate(1 day)
  timezone: UTC
  operation: REINGESTION
```

Use a positive integer with minutes, hours or days:

```text
rate(1 hour)
rate(2 hours)
rate(1 day)
```

Cron expressions, one-time schedules and non-UTC scheduling are not
supported yet.

Initial ingestion starts through an explicit request. Recurring
schedules use `REINGESTION`.

Every task group must use `full-load` when scheduling is enabled.
A recurring schedule must not repeatedly replace an ongoing CDC task.

The compiler checks and includes the schedule in its output. Creating
the scheduler and triggering the workflow come later.

Each scheduled re-ingestion still needs available capacity, ownership
and a prepared fresh extraction location.

## Validate a service

Run from the repository root:

```bash
ingestion-config validate \
  --service modules/database-migration-service/configuration/examples/postgres.yaml \
  --platform modules/database-migration-service/configuration/examples/platform-development.yaml
```

A successful check prints:

```text
VALID: sandbox-postgres
```

Invalid configuration prints an error and returns a non-zero exit code.

## Compile a service

```bash
ingestion-config compile \
  --service modules/database-migration-service/configuration/examples/postgres.yaml \
  --platform modules/database-migration-service/configuration/examples/platform-development.yaml \
  --extraction-generation generation-one \
  --output /tmp/postgres-generation-one.json
```

The output file must not already exist. Choose another filename when
running the command again.

The tool writes the complete JSON file before making it available at
the requested path.

## Generated output

| Field | What it means |
| --- | --- |
| `artifact_version` | Version of the output format |
| `compiler_version` | Compiler version used |
| `naming_version` | Version of the resource-naming rules |
| `configuration_revision` | Hash of the resolved configuration and compiler/format versions |
| `extraction_generation` | The extraction resource generation supplied by deployment |
| `artifact_revision` | Hash of the complete output, excluding this hash field |
| `module_inputs` | Inputs for each task group's `dms-source-ingestion` module |
| `runtime_configuration` | Resolved service settings for the runtime components |

This JSON is input data for the deployment composition. It is not a
standalone Terraform configuration that can be applied directly.

Each task group currently gets a separate set of module inputs.

The deployment work will consume those inputs and publish the actual
resource references after resources have been created.

## Revisions and generations

The deployment process supplies the extraction generation. It is
separate from an ingestion version and stays outside service YAML.

The same configuration and generation produce the same output.

Changing the generation keeps the configuration revision but changes:

- The artifact revision
- Deployment and task names
- The landing prefix

Changing an ordinary setting updates the configuration revision without
automatically renaming resources.

Changes to the schemas or compiler behaviour need an appropriate
compiler version change.

## Landing paths

The landing root includes the configured base prefix, environment,
source, database, task group and extraction generation.

For example:

```text
landing/development/sandbox-postgres/dms_test/integration/generation-one/
```

DMS adds its schema, table and file layout underneath that root.

Generating a different prefix does not confirm that it is unused.
Deployment and runtime checks must prevent unsafe reuse.

Previous contracts, committed historical data and their manifests must
remain accessible when old DMS resources are replaced or removed.

## Before onboarding a source

The service needs:

- Existing source schemas and selected tables
- A supported engine and table/type configuration
- A Secrets Manager secret containing the connection details
- Permissions to read metadata and extract the selected data
- The required database logging and permissions for CDC
- Network access from the deployed components
- Access to the destination bucket and encryption keys
- Suitable keys and supported ordering behaviour for CDC processing

Oracle service and connection details must be available through the
supported source connection configuration.

This tool does not create source databases or configure their logging.

## Run the tests

Install the package and development requirements, then run:

```bash
python -m pytest \
  -c modules/database-migration-service/configuration/pytest.ini \
  modules/database-migration-service/configuration/tests \
  -q \
  -p no:cacheprovider
```

The tests cover configuration loading, platform bindings, schedules,
table selection, naming, compilation, revisions, CLI behaviour and
bundled schemas.

They run without AWS access.

These checks do not prove that a real database is reachable or that DMS
has ingested the selected tables. That needs integration testing.

## Build the package

```bash
python -m pip wheel \
  --no-deps \
  --wheel-dir /tmp/ingestion-config-dist \
  ./modules/database-migration-service/configuration
```

The wheel includes both JSON Schemas so validation works after
installation.

## Deployment resource lookup

The tool can build a deployment manifest from a compiled service
configuration and the resource bindings exported by Terraform.

The manifest records the selected tables, DMS resources and landing
locations. Table lookup checks the requested identity and exact
configuration revisions, then returns the resources and all tables
sharing the DMS task.

Published lookup reads specific S3 object versions and checks their
SHA-256 digests. It can also resolve required contract and downstream
capabilities from a pinned deployment catalog.

Checks reject mismatched bindings, broader table mappings, missing
required capabilities and overlapping deployment boundaries.

The deployment still needs to publish these documents, control which
deployments are available and verify the actual AWS resources. The
Ingestion Registry will store ownership and the references needed for
recovery. These local checks do not prove live ingestion isolation.

## Compiling several services

Real service files belong in the environment repository. Each YAML filename
must match its service name, for example sandbox-postgres.yaml.

Extraction generations are supplied in a separate JSON map keyed by service
name. They are reviewed deployment settings, not timestamps or CI run IDs.
Changing a generation can create a new extraction boundary and DMS resources.

    ingestion-config compile-directory --services services --platform platform.json --generations generations.json --output compiled.auto.tfvars.json

The output contains a compiled_artifacts map for configured-ingestion.
Every service is validated before the output is written. Existing output
files are not overwritten.

Platform bindings can include ssl_mode, certificate_arn and
extra_connection_attributes. Verified TLS modes require a certificate.

After apply, build-deployment-bundle validates the actual resource bindings
across services, including resource reuse and overlapping landing locations.

It prepares the deployment documents. It does not upload them or pin them
to an ingestion in the Registry.

## Preparing another extraction generation

Deployment inventory keys include the source and extraction generation:

    sandbox-postgres/sandbox-v1
    sandbox-postgres/sandbox-v2

When preparing a new generation, pass the previously reviewed inventory
through --retained:

    ingestion-config compile-directory --services services --platform platform.json --generations generations.json --retained retained.json --output next.auto.tfvars.json

Previous generations are kept exactly as compiled. They are not regenerated
from the current service YAML.

Reusing a generation with identical content is harmless. Changing an existing
generation is rejected; prepare a fresh generation instead.

Retained inventory must come from the reviewed deployment records, not an
unverified local file. A checksum detects changed content but does not by
itself establish who authorised it.