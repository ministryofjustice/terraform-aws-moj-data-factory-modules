from dataclasses import replace

import pytest

from ingestion_registry.ownership import IngestionOwnership
from ingestion_registry.task_groups import TaskGroupOwnership

from dms_runtime.control import TaskControl
from dms_runtime.operations import (
    RuntimeOperationConflict,
    RuntimeOperationPending,
    RuntimeOperations,
)
from dms_runtime.ownership import (
    RegistryScopeGuard,
    RuntimeOwnershipLost,
)
from dms_runtime.requests import (
    IngestionOwner,
    TaskOwner,
)

from test_operations import (
    command_result,
    operation_setup,
)
from test_ownership import scope_setup
from test_persisted_control import persisted_setup


def take_over(setup):
    registry = setup["registry"]
    clock = setup["clock"]

    clock["now"] += 300

    ingestion_claim = IngestionOwnership(
        registry,
        clock=lambda: clock["now"],
    ).claim(
        ingestion_id=setup["request"].ingestion_id,
        execution_id="replacement-table-worker",
    )

    task_claim = TaskGroupOwnership(
        registry,
        clock=lambda: clock["now"],
    ).claim(
        ingestion_id=setup["request"].ingestion_id,
        execution_id="replacement-group-coordinator",
    )

    request = replace(
        setup["request"],
        ingestion_owner=IngestionOwner(
            execution_id=ingestion_claim.execution_id,
            fencing_token=ingestion_claim.fencing_token,
            attempt=ingestion_claim.attempt,
        ),
        task_owner=TaskOwner(
            execution_id=task_claim.execution_id,
            group_fencing_token=task_claim.group_fencing_token,
            task_fencing_token=task_claim.task_fencing_token,
        ),
    )

    guard = RegistryScopeGuard(
        registry,
        context=setup["guard"].context,
        ingestion_claim=ingestion_claim,
        task_claim=task_claim,
        clock=lambda: clock["now"],
    )

    operations = RuntimeOperations(guard)

    control = TaskControl(
        setup["dms"],
        setup["inspection"],
        setup["preflight"],
        check_scope=guard,
        operations=operations,
    )

    return request, operations, control


def sequence_record(setup):
    return setup["registry"]._read(
        setup["guard"].task_claim.task_key,
        "RUNTIME_SEQUENCE",
    )


def test_takeover_cannot_redispatch_an_unresolved_operation(
    persisted_setup,
):
    setup = persisted_setup

    admission = setup["operations"].begin(
        setup["request"],
        setup["binding"],
    )
    before = sequence_record(setup)

    request, _, control = take_over(setup)

    with pytest.raises(RuntimeOperationPending):
        control.start(
            setup["binding"],
            request=request,
        )

    assert sequence_record(setup) == before

    stored = setup["registry"]._read(
        admission.record["pk"],
        admission.record["sk"],
    )

    assert stored["status"] == "DISPATCHING"
    assert stored["dispatch_token"] == admission.dispatch_token

    setup["inspection"].task_status.assert_not_called()
    setup["preflight"].run.assert_not_called()
    setup["dms"].start_replication_task.assert_not_called()


def test_takeover_cannot_bypass_pending_command_with_a_new_id(
    persisted_setup,
):
    setup = persisted_setup

    admission = setup["operations"].begin(
        setup["request"],
        setup["binding"],
    )
    before = sequence_record(setup)

    request, _, control = take_over(setup)

    setup["task"].update(
        status="running",
        has_started=True,
    )

    with pytest.raises(RuntimeOperationPending):
        control.stop(
            setup["binding"],
            request=replace(
                request,
                action="stop",
                operation_id="replacement-stop",
            ),
        )

    assert sequence_record(setup) == before

    gate = setup["registry"]._read(
        setup["guard"].task_claim.task_key,
        "RUNTIME_COMMAND",
    )

    assert gate["dispatch_token"] == admission.dispatch_token
    setup["dms"].stop_replication_task.assert_not_called()


def test_previous_owner_cannot_record_result_after_takeover(
    persisted_setup,
):
    setup = persisted_setup

    admission = setup["operations"].begin(
        setup["request"],
        setup["binding"],
    )

    take_over(setup)

    with pytest.raises(RuntimeOwnershipLost):
        setup["operations"].finish(
            setup["request"],
            setup["binding"],
            dispatch_token=admission.dispatch_token,
            result=command_result(setup["binding"]),
        )

    record = setup["registry"]._read(
        admission.record["pk"],
        admission.record["sk"],
    )

    assert record["status"] == "DISPATCHING"

    gate = setup["registry"]._read(
        setup["guard"].task_claim.task_key,
        "RUNTIME_COMMAND",
    )

    assert gate["dispatch_token"] == admission.dispatch_token


def test_takeover_replays_recorded_result_and_preserves_ingestion(
    persisted_setup,
):
    setup = persisted_setup
    registry = setup["registry"]
    ingestion_pk = f"INGESTION#{setup['request'].ingestion_id}"

    original = setup["controller"].start(
        setup["binding"],
        request=setup["request"],
    )

    metadata_before = registry._read(ingestion_pk, "META")
    binding_before = registry._read(ingestion_pk, "BINDING")
    sequence_before = sequence_record(setup)

    setup["inspection"].task_status.reset_mock()
    setup["preflight"].run.reset_mock()

    request, _, control = take_over(setup)

    replay = control.start(
        setup["binding"],
        request=request,
    )

    assert replay == original
    assert sequence_record(setup) == sequence_before
    assert registry._read(
        ingestion_pk,
        "BINDING",
    ) == binding_before

    metadata_after = registry._read(ingestion_pk, "META")

    for field in (
        "ingestion_id",
        "ingestion_version",
        "table_identity",
        "configuration_revision",
        "artifact_revision",
        "extraction_generation",
    ):
        assert metadata_after[field] == metadata_before[field]

    assert metadata_after["attempt_count"] == (
        metadata_before["attempt_count"] + 1
    )

    setup["inspection"].task_status.assert_not_called()
    setup["preflight"].run.assert_not_called()
    assert setup["dms"].start_replication_task.call_count == 1


def test_stale_observation_cannot_reserve_after_completed_command(
    persisted_setup,
):
    setup = persisted_setup
    operations = setup["operations"]

    observation = operations.observe(
        setup["request"],
        setup["binding"],
    )

    other_request = replace(
        setup["request"],
        action="stop",
        operation_id="other-stop",
    )

    admission = operations.begin(
        other_request,
        setup["binding"],
    )

    operations.finish(
        other_request,
        setup["binding"],
        dispatch_token=admission.dispatch_token,
        result=command_result(setup["binding"], "stop"),
    )

    before = sequence_record(setup)

    with pytest.raises(RuntimeOperationConflict):
        operations.begin(
            observation,
            setup["binding"],
        )

    assert operations.lookup(
        setup["request"],
        setup["binding"],
    ) is None

    assert sequence_record(setup) == before


def test_successive_commands_retain_monotonic_task_sequence(
    persisted_setup,
):
    setup = persisted_setup
    operations = setup["operations"]

    first = operations.begin(
        setup["request"],
        setup["binding"],
    )

    operations.finish(
        setup["request"],
        setup["binding"],
        dispatch_token=first.dispatch_token,
        result=command_result(setup["binding"]),
    )

    assert sequence_record(setup)["sequence"] == 1

    second_request = replace(
        setup["request"],
        action="stop",
        operation_id="stop-two",
    )

    second = operations.begin(
        second_request,
        setup["binding"],
    )

    operations.finish(
        second_request,
        setup["binding"],
        dispatch_token=second.dispatch_token,
        result=command_result(setup["binding"], "stop"),
    )

    assert first.record["command_sequence"] == 1
    assert second.record["command_sequence"] == 2
    assert sequence_record(setup)["sequence"] == 2

    assert setup["registry"]._read(
        setup["guard"].task_claim.task_key,
        "RUNTIME_COMMAND",
    ) is None