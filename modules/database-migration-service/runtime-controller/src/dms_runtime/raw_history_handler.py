import hashlib
import json
import re
import time
from copy import deepcopy
from typing import Any

from dms_runtime.downstream_commit_adapter import (
    DownstreamCommitAdapter,
    DownstreamCommitConflict,
)
from dms_runtime.downstream_context import validate_request_context
from ingestion_registry.references import S3ObjectReference


REQUEST_FIELDS = {
    "ingestion_id",
    "deployment",
    "contract",
    "raw_batch",
    "execution_context",
}


def _encode(value, *, limit=128 * 1024):
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError(
            "Raw History requires valid JSON values"
        ) from error

    if len(raw) > limit:
        raise ValueError(
            "Raw History request exceeds its size limit"
        )

    return raw


def _digest(value, field):
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9a-f]{64}", value) is None
    ):
        raise ValueError(
            f"{field} must be a lowercase SHA-256 digest"
        )

    return value


class RawHistoryHandler:
    def __init__(
        self,
        *,
        table,
        resolve_target,
        check_scope,
        lease_seconds=960,
        clock=time.time,
    ):
        if (
            not callable(resolve_target)
            or not callable(check_scope)
        ):
            raise ValueError(
                "A trusted target resolver and scope checker are required"
            )

        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError(
                "lease_seconds must be a positive integer"
            )

        if not callable(clock):
            raise ValueError("clock must be callable")

        self._table = table
        self._resolve_target = resolve_target
        self._check_scope = check_scope
        self._lease_seconds = lease_seconds
        self._clock = clock

    def handle(self, event: Any) -> dict[str, Any]:
        if not isinstance(event, dict):
            raise ValueError(
                "Expected a Raw History request object"
            )

        if (
            type(event.get("request_version")) is not int
            or event["request_version"] != 1
        ):
            raise ValueError(
                "request_version must be the integer 1"
            )

        operation_id = _digest(
            event.get("operation_id"),
            "operation_id",
        )
        action = event.get("action")
        fields = {
            "request_version",
            "action",
            "operation_id",
        }

        if action == "submit_raw_history":
            fields |= {
                "request_fingerprint",
                "request",
            }
        elif action != "lookup_raw_history":
            raise ValueError(
                "Unsupported Raw History action"
            )

        if set(event) != fields:
            raise ValueError(
                "Unexpected Raw History request fields"
            )

        _encode(event, limit=256 * 1024)

        if action == "lookup_raw_history":
            return self._lookup(operation_id)

        fingerprint = _digest(
            event["request_fingerprint"],
            "request_fingerprint",
        )
        request = self._validate_request(
            operation_id,
            fingerprint,
            event["request"],
        )
        target = self._target(request)

        adapter = DownstreamCommitAdapter(
            table=self._table,
            target=target,
            component="raw_history",
            lease_seconds=self._lease_seconds,
            clock=self._clock,
        )

        completion = adapter.submit(
            operation_id=operation_id,
            request_fingerprint=fingerprint,
            request=request,
        )

        return self._response(
            operation_id,
            fingerprint,
            request,
            completion,
        )

    def _validate_request(
        self,
        operation_id,
        fingerprint,
        request,
    ):
        if (
            not isinstance(request, dict)
            or set(request) != REQUEST_FIELDS
        ):
            raise ValueError(
                "Raw History requires pinned inputs and execution context"
            )

        encoded = _encode(request)
        validate_request_context(request)
        S3ObjectReference(**request["raw_batch"])

        expected_id = hashlib.sha256(
            _encode({
                "ingestion_id": request["ingestion_id"],
                "stage": "HISTORY_BASELINE",
            })
        ).hexdigest()

        if operation_id != expected_id:
            raise ValueError(
                "operation_id does not match HISTORY_BASELINE"
            )

        if hashlib.sha256(encoded).hexdigest() != fingerprint:
            raise ValueError(
                "request_fingerprint does not match the request"
            )

        return json.loads(encoded)

    def _target(self, request):
        self._check_scope(deepcopy(request))
        target = self._resolve_target(deepcopy(request))

        if not all(
            callable(getattr(target, method, None))
            for method in ("commit", "lookup")
        ):
            raise ValueError(
                "Raw History has no configured commit target"
            )

        return target

    def _lookup(self, operation_id):
        intent = self._table.get_item(
            Key={"operation_id": operation_id},
            ConsistentRead=True,
        ).get("Item")

        if intent is None:
            return {
                "response_version": 1,
                "operation_id": operation_id,
                "status": "NOT_FOUND",
            }

        if intent.get("component") != "raw_history":
            raise DownstreamCommitConflict(
                "Operation belongs to another component"
            )

        fingerprint = _digest(
            intent.get("request_fingerprint"),
            "request_fingerprint",
        )
        raw = intent.get("request_json")

        if (
            not isinstance(raw, str)
            or len(raw.encode("utf-8")) > 128 * 1024
        ):
            raise DownstreamCommitConflict(
                "Invalid stored Raw History request"
            )

        try:
            request = json.loads(raw)
        except (ValueError, TypeError, RecursionError) as error:
            raise DownstreamCommitConflict(
                "Invalid stored Raw History request"
            ) from error

        request = self._validate_request(
            operation_id,
            fingerprint,
            request,
        )

        if _encode(request).decode("utf-8") != raw:
            raise DownstreamCommitConflict(
                "Stored Raw History request is not canonical"
            )

        target = self._target(request)
        completion = target.lookup(
            operation_id=operation_id,
            request_fingerprint=fingerprint,
            request=request,
        )

        return self._response(
            operation_id,
            fingerprint,
            request,
            completion,
        )

    @staticmethod
    def _response(
        operation_id,
        fingerprint,
        request,
        completion,
    ):
        response = {
            "response_version": 1,
            "operation_id": operation_id,
            "ingestion_id": request["ingestion_id"],
            "request_fingerprint": fingerprint,
            "status": "ACCEPTED",
        }

        if completion is None:
            return response

        if (
            completion.operation_id != operation_id
            or completion.ingestion_id != request["ingestion_id"]
            or completion.request_fingerprint != fingerprint
        ):
            raise DownstreamCommitConflict(
                "Raw History completion belongs to another request"
            )

        reference = dict(completion.result_reference)

        if (
            reference.get("kind") != "raw-history-commit"
            or any(
                not isinstance(key, str)
                or not key
                or not isinstance(value, str)
                or not value.strip()
                for key, value in reference.items()
            )
        ):
            raise DownstreamCommitConflict(
                "Raw History completion requires durable commit evidence"
            )

        try:
            commit = S3ObjectReference(**{
                field: reference[field]
                for field in (
                    "bucket_name",
                    "object_key",
                    "version_id",
                    "sha256",
                )
            })
        except (KeyError, TypeError, ValueError) as error:
            raise DownstreamCommitConflict(
                "Invalid versioned Raw History commit"
            ) from error

        if (
            reference.get("commit_id") != commit.sha256
            or not reference.get(
                "history_location", ""
            ).startswith("s3://")
            or not reference["history_location"].endswith("/")
        ):
            raise DownstreamCommitConflict(
                "Invalid Raw History commit identity"
            )

        return {
            **response,
            "status": "SUCCEEDED",
            "result_reference": reference,
        }