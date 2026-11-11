import pytest
from sqlalchemy import types as sql_types

from ingestion_registry.contract_pinning import (
    ContractSelectionUnavailable,
    CurrentContractPinning,
)
from ingestion_registry.contracts import IngestionContracts
from ingestion_registry.ownership import IngestionOwnership
from ingestion_registry.progress import IngestionProgress
from ingestion_registry.requests import NewIngestionRequest

from test_contract_orchestration_validation_integration import (
    validated_orchestration as validated_orchestration,
    assess,
)
from test_contract_orchestration_generation_integration import (
    connected_orchestration as connected_orchestration,
    prepare,
)
from test_contract_preparation_integration import (
    preparation_setup as preparation_setup,
)
from test_contract_generation_completion_integration import (
    generation_completion as generation_completion,
)
from test_candidate_generation_integration import (
    candidate_generation as candidate_generation,
    generation_adapter as generation_adapter,
)
from test_contract_orchestration_validation_outcomes_integration import (
    activate_baseline,
    approve_prepared,
    change_source_columns,
    id_column,
    prepare_next,
)


def create_ingestion(setup):
    registry = setup["registry"]

    admitted = registry.admit(
        NewIngestionRequest(
            request_id="contract-pinning-ingestion",
            operation="INITIAL_INGESTION",
            table=setup["table"],
            configuration_revision="a" * 64,
            artifact_revision="b" * 64,
            extraction_generation="generation-one",
        )
    )

    clock = {"now": 1000}

    ownership = IngestionOwnership(
        registry,
        clock=lambda: clock["now"],
    )
    claim = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="contract-pinning-execution",
    )

    progress = IngestionProgress(
        registry,
        clock=lambda: clock["now"],
    )
    pinning = CurrentContractPinning(
        lookup=setup["coordinator"].lookup,
        contracts=IngestionContracts(progress),
    )

    return {
        "admitted": admitted,
        "claim": claim,
        "progress": progress,
        "pinning": pinning,
    }


def pin(context):
    return context["pinning"].pin(
        context["claim"],
        update_id="pin-approved-contract",
        expected_revision=0,
    )


def assert_pin_matches_selection(result, selection):
    registered = selection["registered_contract"]
    reference = registered["object_reference"]
    approval = selection["approval_reference"]
    pinned = result["result_reference"]

    assert result["status"] == "SUCCEEDED"
    assert pinned["kind"] == "registered-contract"
    assert pinned["contract_id"] == registered["contract_id"]
    assert pinned["contract_version"] == registered["contract_version"]
    assert pinned["contract_fingerprint"] == (
        registered["contract_fingerprint"]
    )
    assert pinned["contract_bucket_name"] == reference["bucket_name"]
    assert pinned["contract_object_key"] == reference["object_key"]
    assert pinned["contract_object_version_id"] == reference["version_id"]
    assert pinned["contract_object_sha256"] == reference["sha256"]

    assert pinned["approval_bucket_name"] == approval["bucket_name"]
    assert pinned["approval_object_key"] == approval["object_key"]
    assert pinned["approval_object_version_id"] == approval["version_id"]
    assert pinned["approval_object_sha256"] == approval["sha256"]


def test_contract_can_register_before_ingestion_and_then_be_pinned(
    validated_orchestration,
):
    setup = validated_orchestration
    selection = activate_baseline(setup)

    records = setup["registry"].client.scan(
        TableName=setup["registry"].table_name,
        ConsistentRead=True,
    )
    assert not records.get("LastEvaluatedKey")
    assert all(
        item["record_type"]["S"] != "INGESTION"
        for item in records["Items"]
    )
    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 1

    context = create_ingestion(setup)
    result = pin(context)

    assert_pin_matches_selection(result, selection)

    assert context["progress"].read_stage(
        ingestion_id=context["admitted"].ingestion_id,
        stage="CONTRACT",
    ) == result


def test_unapproved_first_candidate_cannot_be_pinned(
    validated_orchestration,
):
    setup = validated_orchestration
    prepared = prepare(setup)

    waiting = assess(setup, prepared)
    assert waiting["status"] == "REVIEW_REQUIRED"

    context = create_ingestion(setup)

    with pytest.raises(
        ContractSelectionUnavailable,
        match="No current approved contract",
    ):
        pin(context)

    assert context["progress"].read_stage(
        ingestion_id=context["admitted"].ingestion_id,
        stage="CONTRACT",
    ) is None

    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 0

    approve_prepared(setup, prepared)
    ready = assess(setup, prepared)

    result = pin(context)

    assert_pin_matches_selection(result, ready["selection"])


def test_running_ingestion_retains_pin_after_actual_new_activation(
    validated_orchestration,
    monkeypatch,
):
    setup = validated_orchestration
    original_selection = activate_baseline(setup)

    context = create_ingestion(setup)
    original_pin = pin(context)
    assert_pin_matches_selection(original_pin, original_selection)

    change_source_columns(
        setup,
        monkeypatch,
        [
            id_column(),
            {
                "name": "description",
                "type": sql_types.String(),
                "nullable": True,
            },
        ],
    )
    prepared = prepare_next(setup, "later-contract-for-future-data")

    waiting = assess(setup, prepared)
    assert waiting["status"] == "REVIEW_REQUIRED"

    approve_prepared(setup, prepared)
    ready = assess(setup, prepared)

    assert ready["status"] == "CONTRACT_READY"
    assert ready["selection"]["registered_contract"][
        "contract_version"
    ] != original_selection["registered_contract"]["contract_version"]

    assert setup["registry"]._read(
        setup["table"].partition_key,
        "CONTRACT_CURRENT",
    ) == ready["selection"]

    assert context["progress"].read_stage(
        ingestion_id=context["admitted"].ingestion_id,
        stage="CONTRACT",
    ) == original_pin

    def unexpected_lookup(**kwargs):
        raise AssertionError(
            "A completed ingestion pin must retain its original contract"
        )

    monkeypatch.setattr(
        setup["coordinator"].lookup,
        "resolve",
        unexpected_lookup,
    )

    repeated = context["pinning"].pin(
        context["claim"],
        update_id="another-delivery",
        expected_revision=1,
    )

    assert repeated == original_pin
    assert_pin_matches_selection(repeated, original_selection)