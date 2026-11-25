import json
import re
from collections.abc import Mapping

from dms_runtime.batch_processing import ProcessingCompletion
from dms_runtime.downstream_context import validate_request_context
from dms_runtime.processing_adapter import BatchProcessingAdapter
from ingestion_registry.references import S3ObjectReference


MAX_PAYLOAD_BYTES = 256 * 1024
COMPONENTS = frozenset({"processing", "raw_history"})


class ComponentTargetUnavailable(RuntimeError):
    """A pinned target has no supported, authorised adapter."""


class ComponentInvocationError(RuntimeError):
    """The invocation failed without verified component completion."""


class ComponentFailed(RuntimeError):
    """A component reported a terminal operation failure."""


class ComponentProtocolError(ValueError):
    """A component response is not valid completion evidence."""


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def _versioned_arn(arn, context):
    prefix = (
        f"arn:aws:lambda:{context.region}:"
        f"{context.account_id}:function:"
    )
    return isinstance(arn, str) and re.fullmatch(
        re.escape(prefix) + r"[A-Za-z0-9_-]{1,64}:[1-9][0-9]*",
        arn,
    ) is not None


def read_component_targets(environment, *, context):
    raw = environment.get("BATCH_COMPONENT_TARGETS")
    if raw is None:
        return {}

    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 2048:
        raise ValueError(
            "BATCH_COMPONENT_TARGETS must fit within 2048 bytes"
        )

    try:
        targets = json.loads(
            raw,
            object_pairs_hook=_json_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError(
            "BATCH_COMPONENT_TARGETS must be valid JSON"
        ) from error

    if (
        not isinstance(targets, dict)
        or not targets
        or set(targets) - COMPONENTS
    ):
        raise ValueError(
            "Configure processing and/or raw_history component targets"
        )

    if environment.get("BATCH_CONTEXT_VERSION") != "1":
        raise ValueError(
            "Configured components require BATCH_CONTEXT_VERSION=1"
        )

    for component, arns in targets.items():
        if (
            not isinstance(arns, list)
            or not arns
            or any(not _versioned_arn(arn, context) for arn in arns)
            or len(set(arns)) != len(arns)
        ):
            raise ValueError(
                f"{component} requires unique numeric-version Lambda "
                "ARNs in the runtime account and region"
            )

    return {
        component: tuple(arns)
        for component, arns in targets.items()
    }


class _LambdaInvocation:
    def __init__(self, client, arn):
        self.client = client
        self.arn = arn

    def __call__(self, event):
        request = event.get("request")
        if request is not None:
            validate_request_context(request)

        encoded = json.dumps(
            event,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

        if len(encoded) > MAX_PAYLOAD_BYTES:
            raise ValueError("Component invocation exceeds 256 KiB")

        response = self.client.invoke(
            FunctionName=self.arn,
            InvocationType="RequestResponse",
            Payload=encoded,
        )

        body = response["Payload"]
        try:
            raw = body.read(MAX_PAYLOAD_BYTES + 1)
        finally:
            body.close()

        if response.get("FunctionError"):
            raise ComponentInvocationError(
                f"Component Lambda {self.arn} failed; inspect its logs"
            )

        if (
            response.get("StatusCode") != 200
            or len(raw) > MAX_PAYLOAD_BYTES
        ):
            raise ComponentProtocolError(
                "Invalid component invocation response"
            )

        try:
            result = json.loads(
                raw,
                object_pairs_hook=_json_object,
                parse_constant=_reject_constant,
            )
        except (
            ValueError,
            TypeError,
            RecursionError,
            UnicodeError,
        ) as error:
            raise ComponentProtocolError(
                "Component returned invalid JSON"
            ) from error

        if not isinstance(result, dict):
            raise ComponentProtocolError(
                "Component must return a response object"
            )

        return result


class LambdaRawHistoryAdapter:
    def __init__(self, invoke):
        self.invoke = invoke

    def submit(
        self,
        *,
        operation_id,
        request_fingerprint,
        request,
    ):
        response = self.invoke({
            "request_version": 1,
            "action": "submit_raw_history",
            "operation_id": operation_id,
            "request_fingerprint": request_fingerprint,
            "request": dict(request),
        })

        return self._read(
            response,
            operation_id=operation_id,
            ingestion_id=request["ingestion_id"],
            fingerprint=request_fingerprint,
        )

    def lookup(self, *, operation_id):
        response = self.invoke({
            "request_version": 1,
            "action": "lookup_raw_history",
            "operation_id": operation_id,
        })

        return self._read(
            response,
            operation_id=operation_id,
        )

    @staticmethod
    def _read(
        response,
        *,
        operation_id,
        ingestion_id=None,
        fingerprint=None,
    ):
        if (
            not isinstance(response, Mapping)
            or type(response.get("response_version")) is not int
            or response["response_version"] != 1
            or response.get("operation_id") != operation_id
        ):
            raise ComponentProtocolError(
                "Raw History response identity is invalid"
            )

        status = response.get("status")

        if status == "NOT_FOUND" and ingestion_id is None:
            return None

        actual_id = response.get("ingestion_id")
        actual_fingerprint = response.get("request_fingerprint")

        if (
            not isinstance(actual_id, str)
            or not actual_id
            or actual_id != actual_id.strip()
            or not isinstance(actual_fingerprint, str)
            or re.fullmatch(
                r"[0-9a-f]{64}",
                actual_fingerprint,
            ) is None
            or (
                ingestion_id is not None
                and actual_id != ingestion_id
            )
            or (
                fingerprint is not None
                and actual_fingerprint != fingerprint
            )
        ):
            raise ComponentProtocolError(
                "Raw History response differs from the operation"
            )

        if status in {"ACCEPTED", "RUNNING"}:
            return None

        if status == "FAILED":
            raise ComponentFailed(
                f"Raw History failed for ingestion {actual_id}, "
                f"operation {operation_id}"
            )

        if status != "SUCCEEDED":
            raise ComponentProtocolError(
                "Unsupported Raw History response status"
            )

        reference = response.get("result_reference")

        if (
            not isinstance(reference, dict)
            or reference.get("kind") != "raw-history-commit"
            or any(
                not isinstance(key, str)
                or not key
                or not isinstance(value, str)
                or not value.strip()
                for key, value in reference.items()
            )
        ):
            raise ComponentProtocolError(
                "Raw History requires durable commit evidence"
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
            raise ComponentProtocolError(
                "Raw History commit must identify an exact S3 version"
            ) from error

        if (
            reference.get("commit_id") != commit.sha256
            or not reference.get(
                "history_location", ""
            ).startswith("s3://")
            or not reference["history_location"].endswith("/")
        ):
            raise ComponentProtocolError(
                "Raw History commit identity is invalid"
            )

        return ProcessingCompletion(
            operation_id=operation_id,
            ingestion_id=actual_id,
            request_fingerprint=actual_fingerprint,
            result_reference=dict(reference),
        )


def build_component_factories(*, client, targets, context):
    if (
        not isinstance(targets, Mapping)
        or set(targets) - COMPONENTS
    ):
        raise ValueError(
            "Unsupported component factory configuration"
        )

    def factory_for(component, allowed):
        allowed = frozenset(allowed)

        def factory(target):
            if (
                not isinstance(target, Mapping)
                or set(target) != {"profile", "kind", "arn"}
                or target.get("kind") != "lambda"
                or not isinstance(target.get("profile"), str)
                or not target["profile"].strip()
                or not _versioned_arn(target.get("arn"), context)
                or target["arn"] not in allowed
            ):
                raise ComponentTargetUnavailable(
                    f"{component} has no authorised "
                    "versioned Lambda target"
                )

            invoke = _LambdaInvocation(client, target["arn"])

            if component == "processing":
                return BatchProcessingAdapter(invoke)

            return LambdaRawHistoryAdapter(invoke)

        return factory

    return {
        component: factory_for(component, allowed)
        for component, allowed in targets.items()
    }