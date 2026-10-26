import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.identity import (
    TaskGroupIdentity,
    identity_digest,
)
from ingestion_registry.ownership import OwnershipClaim
from ingestion_registry.task_groups import TaskGroupClaim

from dms_runtime.ownership import (
    RegistryScopeGuard,
    RuntimeContext,
    RuntimeOwnershipLost,
    RuntimeScopeDenied,
)
from dms_runtime.requests import (
    OWNED_ACTIONS,
    RuntimeRequest,
)


class RuntimeGuardFactory:
    """Validate supplied ownership without claiming or renewing work."""

    def __init__(
        self,
        registry: IngestionRegistry,
        *,
        context: RuntimeContext,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(context, RuntimeContext):
            raise ValueError(
                "context must be a RuntimeContext"
            )

        self.registry = registry
        self.context = context
        self.clock = clock or (lambda: int(time.time()))

    def __call__(
        self,
        request: RuntimeRequest,
        binding: dict[str, Any],
    ) -> RegistryScopeGuard:
        if (
            not isinstance(request, RuntimeRequest)
            or request.action not in OWNED_ACTIONS
        ):
            raise ValueError(
                "An owned runtime request is required"
            )

        if not isinstance(binding, dict):
            raise RuntimeScopeDenied(
                "A resolved binding object is required"
            )

        for field, expected in asdict(self.context).items():
            if binding.get(field) != expected:
                raise RuntimeScopeDenied(
                    f"Binding {field} is outside "
                    "the authorised runtime context"
                )

        try:
            task_arn = binding["resources"]["replication_task_arn"]

            if (
                not isinstance(task_arn, str)
                or not task_arn
            ):
                raise ValueError("Missing task ARN")

            group_key = TaskGroupIdentity(
                environment=binding["environment"],
                source=binding["source"],
                database_name=binding["database_name"],
                task_group=binding["task_group"]["name"],
            ).partition_key

            task_key = "DMSTASK#" + identity_digest([task_arn])

        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeScopeDenied(
                "A complete resolved task binding is required"
            ) from error

        ingestion_record = self.registry._read(
            f"INGESTION#{request.ingestion_id}",
            "OWNERSHIP",
        )
        group_record = self.registry._read(
            group_key,
            "OWNERSHIP",
        )
        task_record = self.registry._read(
            task_key,
            "OWNERSHIP",
        )

        if any(
            record is None
            for record in (
                ingestion_record,
                group_record,
                task_record,
            )
        ):
            raise RuntimeOwnershipLost(
                "Existing ingestion and task ownership are required"
            )

        try:
            ingestion_claim = OwnershipClaim(
                ingestion_id=request.ingestion_id,
                execution_id=request.ingestion_owner.execution_id,
                fencing_token=request.ingestion_owner.fencing_token,
                attempt=request.ingestion_owner.attempt,
                lease_expires_at=int(
                    ingestion_record["lease_expires_at"]
                ),
                recovery_required=ingestion_record[
                    "recovery_required"
                ],
            )

            task_claim = TaskGroupClaim(
                group_key=group_key,
                task_key=task_key,
                task_arn=task_arn,
                execution_id=request.task_owner.execution_id,
                group_fencing_token=(
                    request.task_owner.group_fencing_token
                ),
                task_fencing_token=(
                    request.task_owner.task_fencing_token
                ),
                scope_fingerprint=group_record[
                    "scope_fingerprint"
                ],
                lease_expires_at=min(
                    int(group_record["lease_expires_at"]),
                    int(task_record["lease_expires_at"]),
                ),
                recovery_required=(
                    group_record["recovery_required"]
                    or task_record["recovery_required"]
                ),
            )

        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeOwnershipLost(
                "Registry ownership records are incomplete"
            ) from error

        guard = RegistryScopeGuard(
            self.registry,
            context=self.context,
            ingestion_claim=ingestion_claim,
            task_claim=task_claim,
            clock=self.clock,
        )

        guard(binding, request.action)

        return guard