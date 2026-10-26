import re
from typing import Any

from dms_runtime.control import (
    TaskControl,
    TaskOperationBlocked,
)
from dms_runtime.guard_factory import RuntimeGuardFactory
from dms_runtime.inspection import TaskInspection
from dms_runtime.operations import RuntimeOperations
from dms_runtime.preflight import SourcePreflight
from dms_runtime.requests import (
    OWNED_ACTIONS,
    RuntimeRequest,
)


class OwnedRuntimeActions:
    """Connect trusted deployment dependencies to owned runtime actions."""

    def __init__(
        self,
        *,
        guard_factory: RuntimeGuardFactory,
        dms_client: Any,
        inspection: TaskInspection,
        preflight: SourcePreflight,
        credential_sync_function_arn: str | None,
    ) -> None:
        if not isinstance(guard_factory, RuntimeGuardFactory):
            raise ValueError(
                "guard_factory must be RuntimeGuardFactory"
            )

        context = guard_factory.context

        if credential_sync_function_arn is not None:
            prefix = (
                f"arn:aws:lambda:{context.region}:"
                f"{context.account_id}:function:"
            )

            pattern = (
                re.escape(prefix)
                + r"[A-Za-z0-9_-]{1,64}"
                + r"(?::[A-Za-z0-9_-]+)?"
            )

            if (
                not isinstance(credential_sync_function_arn, str)
                or re.fullmatch(
                    pattern,
                    credential_sync_function_arn,
                ) is None
            ):
                raise ValueError(
                    "Credential sync must identify a Lambda function "
                    "in the authorised account and region"
                )

        self.guard_factory = guard_factory
        self.dms = dms_client
        self.inspection = inspection
        self.preflight = preflight
        self.credential_sync_function_arn = (
            credential_sync_function_arn
        )

    def __call__(
        self,
        request: RuntimeRequest,
        binding: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            not isinstance(request, RuntimeRequest)
            or request.action not in OWNED_ACTIONS
        ):
            raise ValueError(
                "An owned runtime request is required"
            )

        guard = self.guard_factory(request, binding)

        if request.action == "preflight":
            resources = binding["resources"]

            result = self.preflight.run(
                replication_instance_arn=resources[
                    "replication_instance_arn"
                ],
                source_endpoint_arn=resources[
                    "source_endpoint_arn"
                ],
                credential_sync_function_arn=(
                    self.credential_sync_function_arn
                ),
            )

            if result.get("connectionStatus") != "successful":
                raise TaskOperationBlocked(
                    "Source preflight has not succeeded"
                )

            guard(binding, "preflight")

            return {
                "action": "preflight",
                "operation_id": request.operation_id,
                "outcome": "SUCCEEDED",
                "preflight": result,
            }

        control = TaskControl(
            self.dms,
            self.inspection,
            self.preflight,
            check_scope=guard,
            credential_sync_function_arn=(
                self.credential_sync_function_arn
            ),
            operations=RuntimeOperations(guard),
        )

        method = {
            "start": control.start,
            "resume": control.resume,
            "stop": control.stop,
        }[request.action]

        return method(binding, request=request)