from unittest.mock import Mock

import pytest

from dms_runtime.batch_lambda_function import build_batch_entry_point
from ingestion_registry.ownership import OwnershipClaim, OwnershipLost


@pytest.fixture
def batch_entry_setup():
    registry = Mock()
    registry._read.return_value = {
        "execution_id": "recovery-worker",
        "fencing_token": 2,
        "attempt": 2,
        "lease_expires_at": 1100,
        "recovery_required": True,
    }

    handler = Mock()
    handler.handle.return_value = {
        "ingestion_id": "ingestion-one",
        "stage": "PUBLICATION",
        "status": "SUCCEEDED",
    }

    event = {
        "request_version": 1,
        "action": "advance_publication",
        "ingestion_id": "ingestion-one",
        "ingestion_owner": {
            "execution_id": "recovery-worker",
            "fencing_token": 2,
            "attempt": 2,
        },
    }

    entry_point = build_batch_entry_point(
        handler=handler,
        registry=registry,
        clock=lambda: 1000,
    )
    return entry_point, handler, registry, event


def test_dispatches_with_lease_details_from_registry(batch_entry_setup):
    entry_point, handler, registry, event = batch_entry_setup

    assert entry_point(event, None) == handler.handle.return_value

    registry._read.assert_called_once_with(
        "INGESTION#ingestion-one",
        "OWNERSHIP",
    )
    handler.handle.assert_called_once_with(
        {
            "request_version": 1,
            "action": "advance_publication",
            "ingestion_id": "ingestion-one",
        },
        claim=OwnershipClaim(
            ingestion_id="ingestion-one",
            execution_id="recovery-worker",
            fencing_token=2,
            attempt=2,
            lease_expires_at=1100,
            recovery_required=True,
        ),
    )


@pytest.mark.parametrize(
    "record_changes",
    [
        {"lease_expires_at": 1000},
        {"execution_id": "replacement-worker"},
        {"fencing_token": 3},
        {"attempt": 3},
    ],
)
def test_rejects_invalid_claim_before_dispatch(
    batch_entry_setup,
    record_changes,
):
    entry_point, handler, registry, event = batch_entry_setup
    registry._read.return_value.update(record_changes)

    with pytest.raises(OwnershipLost, match="expired or superseded"):
        entry_point(event, None)

    handler.handle.assert_not_called()


def test_rejects_caller_supplied_lease_before_read(batch_entry_setup):
    entry_point, handler, registry, event = batch_entry_setup
    event["ingestion_owner"]["lease_expires_at"] = 9999

    with pytest.raises(ValueError, match="ingestion_owner requires"):
        entry_point(event, None)

    registry._read.assert_not_called()
    handler.handle.assert_not_called()