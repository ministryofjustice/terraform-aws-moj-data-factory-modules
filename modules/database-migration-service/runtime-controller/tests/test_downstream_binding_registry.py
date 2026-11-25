import hashlib
import json
from copy import deepcopy

import pytest

from ingestion_registry.downstream_bindings import (
    DownstreamBindingConflict,
    DownstreamBindings,
)
from ingestion_registry.ownership import OwnershipLost
from ingestion_registry.progress import IngestionProgress
from ingestion_registry.references import S3ObjectReference

from test_ownership import scope_setup


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


@pytest.fixture
def setup(scope_setup):
    registry, original, clock, guard = scope_setup
    claim = guard.ingestion_claim
    binding = deepcopy(original)

    binding["table"].update(
        processing_profile="standard",
        history_profile="standard",
        publication_profile="sandbox",
    )
    binding["component_bindings"] = {}

    pk = f"INGESTION#{claim.ingestion_id}"

    registry.client.update_item(
        TableName=registry.table_name,
        Key=registry._encode(
            {"pk": pk, "sk": "BINDING"}
        ),
        UpdateExpression=(
            "SET binding = :binding,"
            " binding_fingerprint = :fingerprint"
        ),
        ExpressionAttributeValues=registry._encode(
            {
                ":binding": binding,
                ":fingerprint": digest(binding),
            }
        ),
    )

    progress = IngestionProgress(
        registry,
        clock=lambda: clock["now"],
    )

    document = {
        "bucket_name": "example-config",
        "object_key": "completed/batch.json",
        "version_id": "version-one",
        "sha256": "c" * 64,
    }

    for stage, reference in (
        (
            "CONTRACT",
            {
                "kind": "registered-contract",
                "contract_id": "contract-one",
            },
        ),
        (
            "EXTRACTION",
            {"kind": "raw-batch", **document},
        ),
    ):
        progress.record_stage(
            claim,
            stage=stage,
            status="SUCCEEDED",
            update_id=f"fixture-{stage}",
            expected_revision=0,
            result_reference=reference,
        )

    bindings = DownstreamBindings(
        registry,
        clock=lambda: clock["now"],
    )

    reference = S3ObjectReference(
        bucket_name="example-config",
        object_key="published/components.json",
        version_id="catalog-one",
        sha256="d" * 64,
    )

    target = {
        "profile": "standard",
        "kind": "lambda",
        "arn": (
            "arn:aws:lambda:eu-west-2:111122223333:"
            "function:history:3"
        ),
    }

    def pin(**changes):
        arguments = {
            "component": "raw_history",
            "base_binding": binding,
            "catalog_reference": reference,
            "target": target,
        }
        arguments.update(changes)
        return bindings.pin(claim, **arguments)

    return {
        "registry": registry,
        "claim": claim,
        "binding": binding,
        "clock": clock,
        "pk": pk,
        "progress": progress,
        "bindings": bindings,
        "pin": pin,
        "target": target,
    }


def start_history(setup):
    setup["progress"].record_stage(
        setup["claim"],
        stage="HISTORY_BASELINE",
        status="RUNNING",
        update_id="history-started",
        expected_revision=0,
        result_reference={
            "kind": "raw-history-operation",
        },
    )


def test_pin_preserves_extraction_and_replays(setup):
    before = {
        key: setup["registry"]._read(setup["pk"], key)
        for key in ("BINDING", "STAGE#EXTRACTION")
    }

    first = setup["pin"]()

    assert setup["pin"]() == first
    assert first["payload"]["target"] == setup["target"]

    for key, item in before.items():
        assert setup["registry"]._read(
            setup["pk"],
            key,
        ) == item


def test_same_pin_replays_after_history_starts(setup):
    first = setup["pin"]()
    start_history(setup)

    assert setup["pin"]() == first


def test_first_pin_is_blocked_after_history_starts(setup):
    start_history(setup)

    with pytest.raises(DownstreamBindingConflict):
        setup["pin"]()

    assert setup["registry"]._read(
        setup["pk"],
        "DOWNSTREAM_BINDING#raw_history",
    ) is None


def test_expired_owner_cannot_pin(setup):
    setup["clock"]["now"] = (
        setup["claim"].lease_expires_at + 1
    )

    with pytest.raises(OwnershipLost):
        setup["pin"]()


def test_different_target_cannot_replace_pin(setup):
    setup["pin"]()

    changed = {
        **setup["target"],
        "arn": (
            "arn:aws:lambda:eu-west-2:111122223333:"
            "function:history:4"
        ),
    }

    with pytest.raises(DownstreamBindingConflict):
        setup["pin"](target=changed)


@pytest.mark.parametrize(
    "stage",
    ["CONTRACT", "EXTRACTION"],
)
def test_upstream_completion_required(setup, stage):
    registry = setup["registry"]

    registry.client.update_item(
        TableName=registry.table_name,
        Key=registry._encode(
            {
                "pk": setup["pk"],
                "sk": f"STAGE#{stage}",
            }
        ),
        UpdateExpression="SET #status = :status",
        ExpressionAttributeNames={
            "#status": "status",
        },
        ExpressionAttributeValues=registry._encode(
            {":status": "RUNNING"}
        ),
    )

    with pytest.raises(DownstreamBindingConflict):
        setup["pin"]()


def test_lost_write_response_recovers_original_pin(
    setup,
    monkeypatch,
):
    client = setup["registry"].client
    original = client.transact_write_items

    def lose_response(**arguments):
        result = original(**arguments)

        if any(
            "Put" in action
            for action in arguments["TransactItems"]
        ):
            raise TimeoutError(
                "response lost after commit"
            )

        return result

    monkeypatch.setattr(
        client,
        "transact_write_items",
        lose_response,
    )

    with pytest.raises(TimeoutError):
        setup["pin"]()

    stored = setup["registry"]._read(
        setup["pk"],
        "DOWNSTREAM_BINDING#raw_history",
    )

    assert setup["pin"]() == stored