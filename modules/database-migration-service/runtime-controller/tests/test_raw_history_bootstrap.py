import hashlib
import json
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest

from dms_runtime import raw_history_bootstrap as bootstrap
from dms_runtime.ownership import RuntimeContext, RuntimeScopeDenied
from dms_runtime.processing_inputs import ProcessingInputError

from test_downstream_raw_manifest import context_request
from test_processing_inputs import pinned_inputs as pinned_inputs


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


@pytest.fixture
def setup(context_request, monkeypatch):
    _, request, raw = context_request
    request = deepcopy(request)

    s3 = boto3.client("s3", region_name="us-east-1")
    owner = boto3.client("sts").get_caller_identity()["Account"]

    context = RuntimeContext(
        environment="development",
        account_id=owner,
        region="eu-west-2",
        source=request["deployment"]["source"],
        database_name=request["deployment"]["database_name"],
    )
    arn = (
        f"arn:aws:lambda:eu-west-2:{owner}:"
        "function:raw-history:3"
    )

    request["deployment"]["table"]["history_profile"] = "standard"
    request["deployment"]["component_bindings"] = {
        "raw_history": {
            "profile": "standard",
            "kind": "lambda",
            "arn": arn,
        },
    }

    for bucket in (
        "raw-history-config-test",
        "raw-history-bootstrap-test",
    ):
        s3.create_bucket(Bucket=bucket)
        s3.put_bucket_versioning(
            Bucket=bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )

    operations = boto3.resource(
        "dynamodb",
        region_name="eu-west-2",
    ).create_table(
        TableName="raw-history-bootstrap-intents",
        KeySchema=[
            {
                "AttributeName": "operation_id",
                "KeyType": "HASH",
            },
        ],
        AttributeDefinitions=[
            {
                "AttributeName": "operation_id",
                "AttributeType": "S",
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    landing = request["deployment"]["landing"]
    settings = {
        "configuration_version": 1,
        "context": asdict(context),
        "operation_table_name": "raw-history-bootstrap-intents",
        "published_documents": {
            "bucket_name": "example-config",
            "bucket_owner": owner,
            "prefix": "services/",
        },
        "history_bucket": {
            "bucket_name": "raw-history-bootstrap-test",
            "bucket_owner": owner,
        },
        "max_batch_bytes": 1024,
        "tables": [
            {
                "schema_name": (
                    request["deployment"]["table"]["schema_name"]
                ),
                "table_name": (
                    request["deployment"]["table"]["table_name"]
                ),
                "profile": "standard",
                "history_prefix": "example-domain/example/",
                "landings": [
                    {
                        **landing,
                        "bucket_owner": owner,
                    },
                ],
                "manifest_location": {
                    "bucket_name": request["raw_batch"]["bucket_name"],
                    "bucket_owner": owner,
                    "prefix": "manifests/",
                },
                "contract_location": {
                    "bucket_name": (
                        request["contract"]["contract_bucket_name"]
                    ),
                    "bucket_owner": owner,
                    "prefix": "contracts/",
                },
            },
        ],
    }

    environment = {
        "RUNTIME_ENVIRONMENT": context.environment,
        "RUNTIME_ACCOUNT_ID": owner,
        "AWS_REGION": context.region,
        "RUNTIME_SOURCE": context.source,
        "RUNTIME_DATABASE_NAME": context.database_name,
        "INGESTION_REGISTRY_TABLE_NAME": "ingestion-registry",
    }

    registry = Mock()
    registry._read.return_value = {
        "ingestion_id": request["ingestion_id"],
        "status": "SUCCEEDED",
        "result_reference": {
            "kind": "raw-batch",
            **request["raw_batch"],
        },
    }

    lookup = Mock()
    lookup.resolve.return_value = deepcopy(request["deployment"])

    monkeypatch.setattr(
        bootstrap,
        "IngestionRegistry",
        Mock(return_value=registry),
    )
    monkeypatch.setattr(
        bootstrap,
        "PinnedRuntimeLookup",
        Mock(return_value=lookup),
    )

    reader = Mock(
        return_value=deepcopy(request["execution_context"])
    )
    monkeypatch.setattr(
        bootstrap,
        "read_downstream_context",
        reader,
    )

    def publish(value):
        body = json.dumps(value).encode()
        written = s3.put_object(
            Bucket="raw-history-config-test",
            Key="configuration.json",
            Body=body,
        )
        environment["RAW_HISTORY_CONFIGURATION"] = json.dumps({
            "bucket_name": "raw-history-config-test",
            "object_key": "configuration.json",
            "version_id": written["VersionId"],
            "sha256": hashlib.sha256(body).hexdigest(),
        })

    publish(settings)

    event = {
        "request_version": 1,
        "action": "submit_raw_history",
        "operation_id": digest({
            "ingestion_id": request["ingestion_id"],
            "stage": "HISTORY_BASELINE",
        }),
        "request_fingerprint": digest(request),
        "request": request,
    }

    return {
        "settings": settings,
        "environment": environment,
        "arn": arn,
        "publish": publish,
        "event": event,
        "lookup": lookup,
        "registry": registry,
        "reader": reader,
        "operations": operations,
        "s3": s3,
        "raw": raw,
    }


def build(setup):
    return bootstrap.build_handler(
        setup["environment"],
        session=boto3.Session(),
        invoked_function_arn=setup["arn"],
    )


def test_bootstrap_commits_configured_history_and_recovers(setup):
    handler = build(setup)
    first = handler.handle(setup["event"])

    assert first["status"] == "SUCCEEDED"
    assert first["result_reference"]["history_location"] == (
        "s3://raw-history-bootstrap-test/"
        "example-domain/example/version=2/"
    )

    before = setup["s3"].list_object_versions(
        Bucket="raw-history-bootstrap-test"
    )["Versions"]

    lookup = {
        "request_version": 1,
        "action": "lookup_raw_history",
        "operation_id": setup["event"]["operation_id"],
    }

    assert handler.handle(lookup) == first
    assert setup["s3"].list_object_versions(
        Bucket="raw-history-bootstrap-test"
    )["Versions"] == before


@pytest.mark.parametrize(
    "change",
    ["unqualified", "alias", "other-account"],
)
def test_invalid_invocation_target_rejected_before_clients(setup, change):
    arn = setup["arn"]

    if change == "unqualified":
        arn = arn.rsplit(":", 1)[0]
    elif change == "alias":
        arn = arn.rsplit(":", 1)[0] + ":live"
    else:
        arn = arn.replace(
            setup["settings"]["context"]["account_id"],
            "999988887777",
        )

    session = Mock()

    with pytest.raises(ValueError, match="numeric Lambda version"):
        bootstrap.build_handler(
            setup["environment"],
            session=session,
            invoked_function_arn=arn,
        )

    session.client.assert_not_called()


def test_configuration_checksum_is_verified(setup):
    reference = json.loads(
        setup["environment"]["RAW_HISTORY_CONFIGURATION"]
    )
    reference["sha256"] = "f" * 64
    setup["environment"]["RAW_HISTORY_CONFIGURATION"] = json.dumps(
        reference
    )

    with pytest.raises(ProcessingInputError, match="checksum"):
        build(setup)

    assert setup["operations"].scan()["Count"] == 0


def test_registry_ingestion_version_cannot_be_forged(setup):
    event = deepcopy(setup["event"])
    event["request"]["execution_context"]["ingestion_version"] = 3
    event["request_fingerprint"] = digest(event["request"])

    with pytest.raises(RuntimeScopeDenied, match="execution context"):
        build(setup).handle(event)

    assert setup["operations"].scan()["Count"] == 0


@pytest.mark.parametrize(
    "change",
    ["missing-target", "other-version", "other-profile"],
)
def test_pinned_history_target_is_required(setup, change):
    event = deepcopy(setup["event"])
    target = event["request"]["deployment"][
        "component_bindings"
    ]["raw_history"]

    if change == "missing-target":
        event["request"]["deployment"]["component_bindings"] = {}
    elif change == "other-version":
        target["arn"] = target["arn"][:-1] + "4"
    else:
        target["profile"] = "other"

    setup["lookup"].resolve.return_value = deepcopy(
        event["request"]["deployment"]
    )
    event["request_fingerprint"] = digest(event["request"])

    with pytest.raises(RuntimeScopeDenied, match="pinned profile"):
        build(setup).handle(event)

    assert setup["operations"].scan()["Count"] == 0


def test_registry_manifest_must_match_request(setup):
    setup["registry"]._read.return_value[
        "result_reference"
    ]["sha256"] = "d" * 64

    with pytest.raises(
        RuntimeScopeDenied,
        match="committed extraction manifest",
    ):
        build(setup).handle(setup["event"])

    assert setup["operations"].scan()["Count"] == 0


def test_landing_must_be_configured_even_when_pinned(setup):
    event = deepcopy(setup["event"])
    event["request"]["deployment"]["landing"]["prefix"] = (
        "another-generation/"
    )
    setup["lookup"].resolve.return_value = deepcopy(
        event["request"]["deployment"]
    )
    event["request_fingerprint"] = digest(event["request"])

    with pytest.raises(RuntimeScopeDenied, match="Raw landing"):
        build(setup).handle(event)

    assert setup["operations"].scan()["Count"] == 0


@pytest.mark.parametrize(
    "size",
    [True, 0, 64 * 1024 * 1024 + 1],
)
def test_backend_size_limit_is_explicit(setup, size):
    setup["settings"]["max_batch_bytes"] = size
    setup["publish"](setup["settings"])

    with pytest.raises(ValueError, match="64 MiB"):
        build(setup)


def test_operation_store_cannot_be_registry_table(setup):
    setup["settings"]["operation_table_name"] = "ingestion-registry"
    setup["publish"](setup["settings"])

    with pytest.raises(ValueError, match="separate"):
        build(setup)


def test_table_destinations_cannot_overlap(setup):
    row = deepcopy(setup["settings"]["tables"][0])
    row["table_name"] = "another"
    setup["settings"]["tables"].append(row)
    setup["publish"](setup["settings"])

    with pytest.raises(ValueError, match="history prefixes"):
        build(setup)


def test_history_cannot_overlap_raw_inputs(setup):
    row = setup["settings"]["tables"][0]
    setup["settings"]["history_bucket"]["bucket_name"] = (
        row["landings"][0]["bucket_name"]
    )
    row["history_prefix"] = "landing/"
    setup["publish"](setup["settings"])

    with pytest.raises(ValueError, match="input locations"):
        build(setup)


def test_lambda_entry_uses_actual_invocation_arn(monkeypatch):
    handler = Mock()
    handler.handle.return_value = {"status": "ACCEPTED"}
    factory = Mock(return_value=handler)

    monkeypatch.setattr(bootstrap, "_get_handler", factory)

    context = SimpleNamespace(
        invoked_function_arn="arn:example:version"
    )

    assert bootstrap.lambda_handler(
        {"example": True},
        context,
    ) == {"status": "ACCEPTED"}

    factory.assert_called_once_with(context.invoked_function_arn)


def test_configuration_uses_pinned_version_not_latest(setup):
    original = setup["environment"]["RAW_HISTORY_CONFIGURATION"]

    changed = deepcopy(setup["settings"])
    changed["tables"][0]["history_prefix"] = (
        "different-domain/example/"
    )
    setup["publish"](changed)

    setup["environment"]["RAW_HISTORY_CONFIGURATION"] = original
    result = build(setup).handle(setup["event"])

    assert result["result_reference"]["history_location"] == (
        "s3://raw-history-bootstrap-test/"
        "example-domain/example/version=2/"
    )


def test_request_deployment_cannot_override_registry(setup):
    event = deepcopy(setup["event"])
    event["request"]["deployment"]["landing"]["prefix"] = "different/"
    event["request_fingerprint"] = digest(event["request"])

    with pytest.raises(RuntimeScopeDenied, match="pinned deployment"):
        build(setup).handle(event)

    assert setup["operations"].scan()["Count"] == 0


@pytest.mark.parametrize(
    "field",
    ["manifest_location", "contract_location"],
)
def test_input_locations_are_enforced(setup, field):
    setup["settings"]["tables"][0][field]["prefix"] = (
        "not-authorised/"
    )
    setup["publish"](setup["settings"])

    with pytest.raises(
        RuntimeScopeDenied,
        match="configured locations",
    ):
        build(setup).handle(setup["event"])

    assert setup["operations"].scan()["Count"] == 0


def test_deployment_comparison_preserves_json_types(setup):
    setup["lookup"].resolve.return_value["task_group"] = {
        "shared": False,
    }

    event = deepcopy(setup["event"])
    event["request"]["deployment"]["task_group"] = {
        "shared": 0,
    }
    event["request_fingerprint"] = digest(event["request"])

    with pytest.raises(RuntimeScopeDenied, match="pinned deployment"):
        build(setup).handle(event)

    assert setup["operations"].scan()["Count"] == 0