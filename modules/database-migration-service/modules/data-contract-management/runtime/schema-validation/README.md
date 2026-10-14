# Schema validation

This Lambda checks the schema of each incoming Parquet file against a
registered Avro contract. Full load and CDC use the same checks for each file.

It checks field definitions, not individual data values.

## Configuration

| Environment variable | What it is used for |
| --- | --- |
| `SCHEMA_AUDIT_TABLE_NAME` | The table containing registered contracts |
| `VALIDATION_AUDIT_TABLE_NAME` | The table storing validation results |
| `INGESTION_TYPE` | `FULL` or `CDC`, saved with the result |
| `SOURCE_MAPPINGS` | Maps an incoming S3 bucket and path to a source, database, schema and table |

Example:

```json
[
  {
    "bucket_name": "example-incoming-bucket",
    "prefix": "landing/public/example_table/",
    "source": "example-service",
    "database_name": "example_database",
    "schema_name": "public",
    "table_name": "example_table"
  }
]
```

Each mapping identifies one table. The prefix must end with `/` and must
not start with `/`.

Each file must match exactly one mapping. Different paths can point to
the same table, but overlapping mappings are rejected when a file matches
more than one.

Use the actual incoming file paths in the configuration. The path above
is just an example.

## How validation works

1. Resolve the source, database, schema and table from the configured
   bucket and path.
2. Calculate the contract ID using UUID5 from
   `source:database_name:schema_name:table_name`.
3. Read all registry query pages and select the highest numeric contract
   version.
4. Read the registered Avro contract from S3, using its recorded object
   version where available.
5. Check that the contract identity and version match the selected record
   and incoming source.
6. Read the incoming Parquet schema and validate its Avro representation.
7. Put both schemas into the same structure and compare their fingerprints.
8. Save the validation result in the audit table.

The comparison keeps:

- Field names and their order.
- Whether fields can be null.
- Field types and logical types.
- Decimal precision and scale.
- Supported array and nested field details.

Contract metadata, namespaces, descriptions and defaults are excluded
from the comparison.

Unsupported types fail with an error. These include unsigned 64-bit
integers, nanosecond Arrow timestamps and named Avro type references.

DMS-added columns are included in the comparison; they are not
automatically ignored.

The current contract selection uses the highest registered version.
It does not select a historical contract version for an older file.

## Reading files from S3

The incoming file is read using the object version from the S3 event
when supplied. ETag checks also guard against reading changed content.

Events without a version ID must include an ETag and sequencer.

The Parquet reader uses S3 range requests to read the schema metadata.
Each individual read is limited to 64 MiB. This is not a limit on the
whole file size.

## Validation results

Results are stored in a separate DynamoDB table with:

- String partition key: `validation_id`
- No sort key.

The ID is calculated from the bucket, file key and object details in
the event. A retry of the same event uses the same record. Different
files or object versions get separate records.

Each record holds the latest attempt's result. Concurrent attempts can
overwrite the same record.

| Result | Status | Comparison outcome |
| --- | --- | --- |
| Schemas match | `SUCCESS` | `NO_CHANGE` |
| Schemas differ | `FAIL` | `SCHEMA_CHANGED` |
| Comparison could not finish | `FAIL` | `NOT_COMPLETED` |

Results include the file details, available contract details, comparison
fingerprints where calculated, timestamp and outcome. Schema mismatches
also include the differences found.

Failures are recorded where the incoming event can be identified and
the audit table can be written.

The Lambda raises an error on failure and stops processing the remaining
records in that invocation. Other invocations and DMS tasks are unaffected.

## Testing

`test_main.py` uses pytest, mocked AWS calls and Parquet files created
in memory.

The tests cover:

- Matching schemas for FULL and CDC.
- Decimal precision/scale, timestamp precision, nullability and nested
  type changes.
- Contract metadata being excluded from schema comparison.
- Version-specific S3 reads.
- Missing contracts and mismatched contract identity or version.
- Unmapped incoming files.
- Audit results and repeated-event validation IDs.
- S3 and DynamoDB failures.
- Contract selection across multiple query pages.
- Unsupported types and invalid events.

The current suite passed all 32 tests under Python 3.12 ARM64.