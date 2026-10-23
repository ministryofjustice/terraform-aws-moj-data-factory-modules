import copy

import pytest
from botocore.exceptions import ReadTimeoutError

from ingestion_registry.identity import TableIdentity
from ingestion_registry.ownership import OwnershipLost
from ingestion_registry.requests import NewIngestionRequest
from ingestion_registry.resource_bindings import BindingConflict


def test_binding_and_boundary_are_recorded_together(
    binding_setup,
) -> None:
    registry, _, admitted, _, claim, bindings, binding = binding_setup

    result = bindings.pin(claim, binding)

    stored = registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "BINDING",
    )
    reservation = registry._read(
        result["boundary_key"],
        "RESERVATION",
    )

    assert stored["binding"] == binding
    assert reservation["ingestion_id"] == admitted.ingestion_id


def test_repeated_binding_returns_original_record(
    binding_setup,
) -> None:
    _, _, _, clock, claim, bindings, binding = binding_setup

    first = bindings.pin(claim, binding)
    clock["now"] += 10

    assert bindings.pin(claim, binding) == first


def test_pinned_binding_cannot_be_replaced(
    binding_setup,
) -> None:
    _, _, _, _, claim, bindings, binding = binding_setup

    bindings.pin(claim, binding)

    changed = copy.deepcopy(binding)
    changed["resources"]["replication_task_id"] = "another-task"

    with pytest.raises(BindingConflict, match="different pinned binding"):
        bindings.pin(claim, changed)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("source", "another-service"),
        ("configuration_revision", "e" * 64),
        ("artifact_revision", "f" * 64),
        ("extraction_generation", "generation-two"),
    ],
)
def test_binding_must_match_admitted_request(
    binding_setup,
    field,
    replacement,
) -> None:
    _, _, _, _, claim, bindings, binding = binding_setup
    binding[field] = replacement

    with pytest.raises(BindingConflict, match=field):
        bindings.pin(claim, binding)


def test_recovery_reuses_original_binding(
    binding_setup,
) -> None:
    _, ownership, admitted, clock, claim, bindings, binding = (
        binding_setup
    )

    original = bindings.pin(claim, binding)
    clock["now"] = claim.lease_expires_at

    recovered = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-two",
    )

    assert bindings.pin(recovered, binding) == original

    with pytest.raises(OwnershipLost):
        bindings.pin(claim, binding)


def test_invalid_binding_creates_no_reservation(
    binding_setup,
) -> None:
    registry, _, admitted, _, claim, bindings, binding = binding_setup
    binding["landing"]["prefix"] = "landing/../unsafe/"

    with pytest.raises(BindingConflict, match="landing"):
        bindings.pin(claim, binding)

    assert registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "BINDING",
    ) is None


def admit_another_table_ingestion(
    registry,
    ownership,
    binding,
    *,
    source,
    table_name,
    request_id,
):
    admitted = registry.admit(
        NewIngestionRequest(
            request_id=request_id,
            operation="INITIAL_INGESTION",
            table=TableIdentity(
                environment=binding["environment"],
                source=source,
                database_name=binding["database_name"],
                schema_name=binding["table"]["schema_name"],
                table_name=table_name,
            ),
            configuration_revision=binding["configuration_revision"],
            artifact_revision=binding["artifact_revision"],
            extraction_generation=binding["extraction_generation"],
        )
    )

    claim = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id=f"execution-{request_id}",
    )

    other_binding = copy.deepcopy(binding)
    other_binding["source"] = source
    other_binding["table"]["table_name"] = table_name

    return admitted, claim, other_binding


def test_another_ingestion_cannot_reuse_reserved_table_location(
    binding_setup,
) -> None:
    registry, ownership, _, _, claim, bindings, binding = binding_setup

    original = bindings.pin(claim, binding)

    admitted, other_claim, other_binding = admit_another_table_ingestion(
        registry,
        ownership,
        binding,
        source="another-service",
        table_name=binding["table"]["table_name"],
        request_id="another-service-request",
    )

    with pytest.raises(BindingConflict, match="reservation"):
        bindings.pin(other_claim, other_binding)

    assert registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "BINDING",
    ) is None

    reservation = registry._read(
        original["boundary_key"],
        "RESERVATION",
    )
    assert reservation["ingestion_id"] == claim.ingestion_id


def test_different_tables_can_share_task_group_landing_root(
    binding_setup,
) -> None:
    registry, ownership, _, _, claim, bindings, binding = binding_setup

    binding["task_group"]["tables"].append(
        {
            "schema_name": binding["table"]["schema_name"],
            "table_name": "another_table",
        }
    )

    first = bindings.pin(claim, binding)

    admitted, other_claim, other_binding = admit_another_table_ingestion(
        registry,
        ownership,
        binding,
        source=binding["source"],
        table_name="another_table",
        request_id="another-table-request",
    )

    second = bindings.pin(other_claim, other_binding)

    assert first["binding"]["landing"] == second["binding"]["landing"]
    assert first["boundary_key"] != second["boundary_key"]
    assert second["ingestion_id"] == admitted.ingestion_id


def test_competing_reservation_rolls_back_losing_binding(
    binding_setup,
    monkeypatch,
) -> None:
    registry, ownership, _, _, claim, bindings, binding = binding_setup

    admitted, other_claim, other_binding = admit_another_table_ingestion(
        registry,
        ownership,
        binding,
        source="another-service",
        table_name=binding["table"]["table_name"],
        request_id="competing-reservation",
    )

    original_write = registry.client.transact_write_items
    raced = False
    winner = None

    def reserve_then_write(**kwargs):
        nonlocal raced, winner

        if not raced:
            raced = True
            winner = bindings.pin(other_claim, other_binding)

        return original_write(**kwargs)

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        reserve_then_write,
    )

    with pytest.raises(BindingConflict):
        bindings.pin(claim, binding)

    assert winner is not None
    assert registry._read(
        f"INGESTION#{claim.ingestion_id}",
        "BINDING",
    ) is None

    reservation = registry._read(
        winner["boundary_key"],
        "RESERVATION",
    )
    assert reservation["ingestion_id"] == admitted.ingestion_id


def test_lost_binding_response_preserves_original_reservation(
    binding_setup,
    monkeypatch,
) -> None:
    registry, _, admitted, clock, claim, bindings, binding = binding_setup
    original_write = registry.client.transact_write_items

    def commit_then_timeout(**kwargs):
        original_write(**kwargs)
        raise ReadTimeoutError(
            endpoint_url=registry.client.meta.endpoint_url
        )

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        commit_then_timeout,
    )

    with pytest.raises(ReadTimeoutError):
        bindings.pin(claim, binding)

    committed = registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "BINDING",
    )
    assert committed is not None

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        original_write,
    )
    clock["now"] += 10

    repeated = bindings.pin(claim, binding)

    assert repeated == committed

    reservation = registry._read(
        committed["boundary_key"],
        "RESERVATION",
    )
    assert reservation["ingestion_id"] == admitted.ingestion_id


def test_returned_binding_cannot_modify_stored_snapshot(
    binding_setup,
) -> None:
    registry, _, admitted, _, claim, bindings, binding = binding_setup

    result = bindings.pin(claim, binding)
    result["binding"]["landing"]["prefix"] = "changed/"

    stored = registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "BINDING",
    )

    assert stored["binding"]["landing"]["prefix"] == (
        binding["landing"]["prefix"]
    )