import json
from collections import deque
from pathlib import Path

import pytest

from dms_runtime.batch_downstream import BatchPrerequisiteMissing


WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "batch-downstream.asl.json"
)

INGESTION_ID = "ingestion-one"
OWNER = {
    "execution_id": "table-worker",
    "fencing_token": 1,
    "attempt": 1,
}
STAGES = {
    "advance_processing": "PROCESSING",
    "advance_raw_history": "HISTORY_BASELINE",
    "advance_publication": "PUBLICATION",
    "mark_baseline_ready": "BASELINE",
}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate ASL key: {key}")
        result[key] = value
    return result


@pytest.fixture
def workflow():
    return json.loads(
        WORKFLOW_PATH.read_text(encoding="utf-8"),
        object_pairs_hook=unique_object,
    )


def read_path(data, path):
    if path == "$":
        return data
    assert path.startswith("$."), path

    for key in path[2:].split("."):
        data = data[key]
    return data


def render_parameters(template, data, *, item=None):
    result = {}
    for key, value in template.items():
        if key.endswith(".$"):
            result[key[:-2]] = (
                item if value == "$$.Map.Item.Value" else read_path(data, value)
            )
        elif isinstance(value, dict):
            result[key] = render_parameters(value, data, item=item)
        else:
            result[key] = value
    return result


def write_result(data, state, result):
    path = state.get("ResultPath", "$")
    if path is None:
        return data
    if path == "$":
        return result

    assert path.startswith("$.") and "." not in path[2:], path
    return {**data, path[2:]: result}


def walk_states(definition, data, invoke):
    """Exercise the ASL states used by this workflow with scripted tasks."""
    name = definition["StartAt"]

    for _ in range(100):
        state = definition["States"][name]
        kind = state["Type"]

        if kind == "Choice":
            name = next(
                (
                    choice["Next"]
                    for choice in state["Choices"]
                    if read_path(data, choice["Variable"])
                    == choice["StringEquals"]
                ),
                state["Default"],
            )
            continue

        if kind == "Pass":
            data = write_result(data, state, state["Result"])
        elif kind == "Task":
            assert state["Resource"] == "${batch_lambda_arn}"
            task_input = read_path(data, state.get("InputPath", "$"))
            if "Parameters" in state:
                task_input = render_parameters(state["Parameters"], task_input)
            data = write_result(data, state, invoke(task_input))
        elif kind == "Map":
            assert state["MaxConcurrency"] == 2
            items = read_path(data, state["ItemsPath"])
            assert len(items) <= state["MaxConcurrency"]
            branches = [
                walk_states(
                    state["ItemProcessor"],
                    render_parameters(state["ItemSelector"], data, item=item),
                    invoke,
                )
                for item in items
            ]
            results = {}
            active = list(enumerate(branches))
            while active:
                waiting = []
                for index, branch in active:
                    try:
                        next(branch)
                    except StopIteration as completed:
                        results[index] = completed.value
                    else:
                        waiting.append((index, branch))
                active = waiting
            data = write_result(
                data, state, [results[index] for index in range(len(items))]
            )
        elif kind == "Wait":
            assert state["Seconds"] > 0
            yield
        elif kind == "Succeed":
            return data
        elif kind == "Fail":
            raise RuntimeError(state["Error"])
        else:
            raise AssertionError(f"Unsupported test state: {kind}")

        if state.get("End") is True:
            return data
        name = state["Next"]

    raise AssertionError("Workflow exceeded the test transition budget")


class ScriptedBatch:
    def __init__(self, **responses):
        self.responses = {
            action: deque(results) for action, results in responses.items()
        }
        self.calls = []

    def invoke(self, event):
        action = event["action"]
        assert event == {
            "request_version": 1,
            "action": action,
            "ingestion_id": INGESTION_ID,
            "ingestion_owner": OWNER,
        }
        self.calls.append(action)
        pending = self.responses.get(action)
        assert pending, f"Unexpected call to {action}"
        outcome = pending.popleft()

        if isinstance(outcome, Exception):
            raise outcome

        return {
            "ingestion_id": INGESTION_ID,
            "stage": STAGES[action],
            "status": outcome,
        }


def run_workflow(workflow, batch, *, mode):
    execution = walk_states(
        workflow,
        {
            "mode": mode,
            "ingestion_id": INGESTION_ID,
            "ingestion_owner": OWNER,
        },
        batch.invoke,
    )

    while True:
        try:
            next(execution)
        except StopIteration as completed:
            return completed.value


def test_publication_waits_for_processing_and_raw_history(workflow):
    batch = ScriptedBatch(
        advance_processing=["RUNNING", "SUCCEEDED"],
        advance_raw_history=["SUCCEEDED"],
        advance_publication=["SUCCEEDED"],
        mark_baseline_ready=["SUCCEEDED"],
    )

    result = run_workflow(workflow, batch, mode="batch")

    assert batch.calls == [
        "advance_processing",
        "advance_raw_history",
        "advance_processing",
        "advance_publication",
        "mark_baseline_ready",
    ]
    assert result == {
        "ingestion_id": INGESTION_ID,
        "stage": "BASELINE",
        "status": "SUCCEEDED",
    }


def test_publication_recovery_bypasses_upstream_stages(workflow):
    batch = ScriptedBatch(
        advance_publication=["RUNNING", "SUCCEEDED"],
        mark_baseline_ready=["SUCCEEDED"],
    )

    result = run_workflow(workflow, batch, mode="resume_publication")

    assert batch.calls == [
        "advance_publication",
        "advance_publication",
        "mark_baseline_ready",
    ]
    assert result["status"] == "SUCCEEDED"


def test_required_reconciliation_blocks_baseline_readiness(workflow):
    batch = ScriptedBatch(
        advance_publication=[
            BatchPrerequisiteMissing("RECONCILIATION has not succeeded")
        ],
    )

    with pytest.raises(BatchPrerequisiteMissing, match="RECONCILIATION"):
        run_workflow(workflow, batch, mode="resume_publication")

    assert batch.calls == ["advance_publication"]


def test_lost_publication_response_can_resume_at_publication(workflow):
    batch = ScriptedBatch(
        advance_processing=["SUCCEEDED"],
        advance_raw_history=["SUCCEEDED"],
        advance_publication=[TimeoutError("response lost"), "SUCCEEDED"],
        mark_baseline_ready=["SUCCEEDED"],
    )

    with pytest.raises(TimeoutError, match="response lost"):
        run_workflow(workflow, batch, mode="batch")

    assert batch.calls[-1] == "advance_publication"
    calls_before_recovery = len(batch.calls)

    result = run_workflow(workflow, batch, mode="resume_publication")

    assert batch.calls[calls_before_recovery:] == [
        "advance_publication",
        "mark_baseline_ready",
    ]
    assert result["status"] == "SUCCEEDED"