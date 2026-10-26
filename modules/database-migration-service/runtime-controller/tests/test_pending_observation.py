from dataclasses import replace

import pytest

from dms_runtime.operations import RuntimeOperationPending

from test_operations import operation_setup
from test_ownership import scope_setup
from test_persisted_control import persisted_setup


def test_observation_is_blocked_while_another_command_is_pending(
    persisted_setup,
):
    setup = persisted_setup

    setup["operations"].begin(
        setup["request"],
        setup["binding"],
    )

    another_request = replace(
        setup["request"],
        action="stop",
        operation_id="another-stop",
    )

    with pytest.raises(RuntimeOperationPending):
        setup["operations"].observe(
            another_request,
            setup["binding"],
        )


def test_pending_command_blocks_another_start_before_preflight(
    persisted_setup,
):
    setup = persisted_setup

    setup["operations"].begin(
        setup["request"],
        setup["binding"],
    )

    another_request = replace(
        setup["request"],
        operation_id="another-start",
    )

    with pytest.raises(RuntimeOperationPending):
        setup["controller"].start(
            setup["binding"],
            request=another_request,
        )

    setup["inspection"].task_status.assert_not_called()
    setup["preflight"].run.assert_not_called()
    setup["dms"].start_replication_task.assert_not_called()