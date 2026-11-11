from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dms_runtime.batch_handler import BatchHandler


@pytest.fixture
def handler_setup():
    processing = Mock()
    raw_history = Mock()
    publication = Mock()
    lifecycle = Mock()
    claim = SimpleNamespace(ingestion_id="ingestion-one")

    handler = BatchHandler(
        processing=processing,
        raw_history=raw_history,
        publication=publication,
        lifecycle=lifecycle,
        reconciliation_required=True,
    )
    return handler, claim, processing, raw_history, publication, lifecycle


def request(action, *, ingestion_id="ingestion-one"):
    return {
        "request_version": 1,
        "action": action,
        "ingestion_id": ingestion_id,
    }


def test_publication_recovery_calls_only_publication(handler_setup):
    handler, claim, processing, raw_history, publication, lifecycle = (
        handler_setup
    )
    publication.advance.return_value = {
        "stage": "PUBLICATION",
        "status": "RUNNING",
        "result_reference": {"operation_id": "publication-one"},
    }

    result = handler.handle(request("advance_publication"), claim=claim)

    assert result == {
        "ingestion_id": "ingestion-one",
        "stage": "PUBLICATION",
        "status": "RUNNING",
    }
    publication.advance.assert_called_once_with(claim)
    processing.advance.assert_not_called()
    raw_history.advance.assert_not_called()
    lifecycle.mark_baseline_ready.assert_not_called()


def test_baseline_uses_configured_reconciliation_requirement(handler_setup):
    handler, claim, _, _, publication, lifecycle = handler_setup

    result = handler.handle(request("mark_baseline_ready"), claim=claim)

    lifecycle.mark_baseline_ready.assert_called_once_with(
        claim,
        reconciliation_required=True,
    )
    publication.advance.assert_not_called()
    assert result == {
        "ingestion_id": "ingestion-one",
        "stage": "BASELINE",
        "status": "SUCCEEDED",
    }


def test_ingestion_must_match_claim_before_stage_is_called(handler_setup):
    handler, claim, processing, _, _, _ = handler_setup

    with pytest.raises(ValueError, match="must match the ownership claim"):
        handler.handle(
            request("advance_processing", ingestion_id="other-ingestion"),
            claim=claim,
        )

    processing.advance.assert_not_called()