import argparse
import json

import boto3
from botocore.config import Config

from ingestion_config.published_documents import DocumentReference
from ingestion_config.published_resolver import TableLookup, resolve_published_table
from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.deployments import DeploymentCatalog
from ingestion_registry.identity import TableIdentity
from ingestion_registry.ownership import IngestionOwnership
from ingestion_registry.requests import NewIngestionRequest
from ingestion_registry.resource_bindings import IngestionResourceBindings
from ingestion_registry.task_groups import TaskGroupOwnership


def invoke(client, function, event):
    response = client.invoke(
        FunctionName=function,
        InvocationType="RequestResponse",
        Payload=json.dumps(event).encode("utf-8"),
    )
    with response["Payload"] as payload:
        result = json.load(payload)
    if response.get("FunctionError"):
        raise RuntimeError(f"{function} failed: {json.dumps(result)}")
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Preview or start a contract-enabled development ingestion."
    )
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--source", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--artifact-revision", required=True)
    parser.add_argument("--catalog-sha256", required=True)
    parser.add_argument("--documents-bucket", required=True)
    parser.add_argument("--contract-lambda", required=True)
    parser.add_argument("--runtime-lambda", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument(
        "--operation",
        choices=("INITIAL_INGESTION", "RE_INGESTION"),
        default="INITIAL_INGESTION",
    )
    parser.add_argument("--start", action="store_true")
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    caller = session.client("sts").get_caller_identity()
    if caller["Account"] != args.account_id:
        raise SystemExit("AWS session is in the wrong account")

    scope = {
        "environment": "development",
        "account_id": args.account_id,
        "region": args.region,
        "source": args.source,
    }
    registry = IngestionRegistry(
        session.client("dynamodb"),
        table_name="data-factory-moj-development-ingestion-registry",
    )
    deployment = DeploymentCatalog(registry).get(
        **scope,
        extraction_generation=args.generation,
        artifact_revision=args.artifact_revision,
        catalog_sha256=args.catalog_sha256,
    )
    if deployment is None:
        raise SystemExit("Published development deployment was not found")

    table = TableIdentity(
        environment="development",
        source=args.source,
        database_name=args.database,
        schema_name=args.schema,
        table_name=args.table,
    )
    lookup = TableLookup(
        **scope,
        database_name=args.database,
        schema_name=args.schema,
        table_name=args.table,
        configuration_revision=deployment["configuration_revision"],
        artifact_revision=args.artifact_revision,
    )
    references = deployment["document_references"]
    binding = resolve_published_table(
        session.client("s3"),
        manifest_reference=DocumentReference(**references["manifest"]),
        artifact_reference=DocumentReference(**references["artifact"]),
        catalog_reference=DocumentReference(**references["catalog"]),
        allowed_bucket=args.documents_bucket,
        allowed_prefix=f"services/{args.source}/",
        expected_bucket_owner=args.account_id,
        lookup=lookup,
    )
    if binding["extraction_generation"] != args.generation:
        raise SystemExit("Published binding identifies a different generation")

    print(json.dumps({
        "request_id": args.request_id,
        "execution_id": args.execution_id,
        "operation": args.operation,
        "table": binding["table"],
        "task_group": binding["task_group"],
        "replication_task_arn": binding["resources"]["replication_task_arn"],
        "extraction_generation": binding["extraction_generation"],
    }, indent=2))

    if not args.start:
        return

    if binding["task_group"]["shared"]:
        raise SystemExit("This operator command requires a single-table task group")

    if input(f"Type {args.request_id} to start DMS: ").strip() != args.request_id:
        raise SystemExit("Start cancelled")

    admitted = registry.admit(
        NewIngestionRequest(
            request_id=args.request_id,
            operation=args.operation,
            table=table,
            configuration_revision=lookup.configuration_revision,
            artifact_revision=lookup.artifact_revision,
            extraction_generation=args.generation,
        )
    )
    ingestion_id = admitted.ingestion_id
    print(f"Ingestion ID: {ingestion_id}", flush=True)

    claim = IngestionOwnership(registry).claim(
        ingestion_id=ingestion_id,
        execution_id=args.execution_id,
        lease_seconds=900,
    )
    IngestionResourceBindings(registry).pin(claim, binding)
    task_owner = TaskGroupOwnership(registry).claim(
        ingestion_id=ingestion_id,
        execution_id=args.execution_id,
        lease_seconds=900,
    )

    lambda_client = session.client(
        "lambda",
        config=Config(
            read_timeout=360,
            retries={"total_max_attempts": 1},
        ),
    )
    owner = {
        "execution_id": claim.execution_id,
        "fencing_token": claim.fencing_token,
        "attempt": claim.attempt,
    }
    pin = invoke(lambda_client, args.contract_lambda, {
        "request_version": 1,
        "operation": "PIN_CONTRACT",
        "ingestion_id": ingestion_id,
        **owner,
    })
    if (
        pin.get("stage") != "CONTRACT"
        or pin.get("status") != "SUCCEEDED"
        or pin.get("result_reference", {}).get("kind") != "registered-contract"
    ):
        raise RuntimeError(f"Contract pin did not succeed: {json.dumps(pin)}")

    print(json.dumps({"contract_pin": pin["result_reference"]}, indent=2), flush=True)

    start = invoke(lambda_client, args.runtime_lambda, {
        "request_version": 1,
        "action": "start",
        "ingestion_id": ingestion_id,
        "operation_id": f"{args.request_id}-start",
        "ingestion_owner": owner,
        "task_owner": {
            "execution_id": task_owner.execution_id,
            "group_fencing_token": task_owner.group_fencing_token,
            "task_fencing_token": task_owner.task_fencing_token,
        },
    })
    print(json.dumps({"dms_start": start}, indent=2))


if __name__ == "__main__":
    main()