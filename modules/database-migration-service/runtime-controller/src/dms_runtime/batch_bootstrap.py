import os
from collections.abc import Callable, Mapping
from functools import lru_cache
from typing import Any

import boto3

from dms_runtime.batch_downstream import BatchDownstream
from dms_runtime.batch_handler import BatchHandler
from dms_runtime.batch_lambda_function import build_batch_entry_point
from dms_runtime.batch_processing import (
    BatchProcessing,
    BatchPublication,
    BatchRawHistory,
    ProcessingAdapter,
)
from dms_runtime.lookup import PinnedRuntimeLookup
from dms_runtime.ownership import RuntimeContext
from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.lifecycle import IngestionLifecycle
from ingestion_registry.ownership import OwnershipClaim
from ingestion_registry.progress import IngestionProgress


class BatchComponentUnavailable(RuntimeError):
    """The batch action has no configured adapter for its pinned target."""


AdapterFactory = Callable[[Mapping[str, Any]], ProcessingAdapter]


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed setting")
    return value


class _ConfiguredBatchStage:
    def __init__(
        self,
        *,
        component: str,
        stage_type: type,
        lookup: PinnedRuntimeLookup,
        progress: IngestionProgress,
        adapter_factory: AdapterFactory | None,
        reconciliation_required: bool,
    ) -> None:
        self._component = component
        self._stage_type = stage_type
        self._lookup = lookup
        self._progress = progress
        self._adapter_factory = adapter_factory
        self._reconciliation_required = reconciliation_required

    def advance(self, claim: OwnershipClaim) -> dict[str, Any]:
        if self._adapter_factory is None:
            raise BatchComponentUnavailable(
                f"Ingestion {claim.ingestion_id}: "
                f"{self._component} has no configured adapter"
            )

        deployment = self._lookup.resolve(claim.ingestion_id)
        target = deployment.get("component_bindings", {}).get(self._component)

        if not isinstance(target, dict) or not target:
            raise BatchComponentUnavailable(
                f"Ingestion {claim.ingestion_id}: "
                f"{self._component} has no pinned target"
            )

        downstream = BatchDownstream(
            resolve_deployment=lambda _: deployment,
            read_stage=self._progress.read_stage,
        )
        options = {
            "downstream": downstream,
            "progress": self._progress,
            "adapter": self._adapter_factory(target),
        }
        if self._stage_type is BatchPublication:
            options["reconciliation_required"] = self._reconciliation_required

        return self._stage_type(**options).advance(claim)


def build_handler(
    environment: Mapping[str, str],
    *,
    session: Any,
    adapter_factories: Mapping[str, AdapterFactory] | None = None,
) -> Callable[[Any, Any], dict[str, str | int]]:
    context = RuntimeContext(
        environment=_required(environment, "RUNTIME_ENVIRONMENT"),
        account_id=_required(environment, "RUNTIME_ACCOUNT_ID"),
        region=_required(environment, "AWS_REGION"),
        source=_required(environment, "RUNTIME_SOURCE"),
        database_name=_required(environment, "RUNTIME_DATABASE_NAME"),
    )
    reconciliation = _required(environment, "BATCH_RECONCILIATION_REQUIRED")
    if reconciliation not in {"true", "false"}:
        raise ValueError("BATCH_RECONCILIATION_REQUIRED must be true or false")
    reconciliation_required = reconciliation == "true"

    registry = IngestionRegistry(
        session.client("dynamodb", region_name=context.region),
        table_name=_required(environment, "INGESTION_REGISTRY_TABLE_NAME"),
    )
    lookup = PinnedRuntimeLookup(
        registry,
        session.client("s3", region_name=context.region),
        context=context,
        allowed_bucket=_required(environment, "PUBLISHED_DOCUMENT_BUCKET"),
        allowed_prefix=_required(environment, "PUBLISHED_DOCUMENT_PREFIX"),
        expected_bucket_owner=_required(
            environment, "PUBLISHED_DOCUMENT_BUCKET_OWNER"
        ),
    )
    progress = IngestionProgress(registry)
    factories = dict(adapter_factories or {})

    def stage(component: str, stage_type: type) -> _ConfiguredBatchStage:
        return _ConfiguredBatchStage(
            component=component,
            stage_type=stage_type,
            lookup=lookup,
            progress=progress,
            adapter_factory=factories.get(component),
            reconciliation_required=reconciliation_required,
        )

    handler = BatchHandler(
        processing=stage("processing", BatchProcessing),
        raw_history=stage("raw_history", BatchRawHistory),
        publication=stage("publication", BatchPublication),
        lifecycle=IngestionLifecycle(progress),
        reconciliation_required=reconciliation_required,
    )
    return build_batch_entry_point(
        handler=handler,
        registry=registry,
        check_scope=lookup.resolve,
    )


@lru_cache(maxsize=1)
def _get_handler() -> Callable[[Any, Any], dict[str, str | int]]:
    return build_handler(os.environ, session=boto3.session.Session())


def lambda_handler(event: Any, context: Any) -> dict[str, str | int]:
    return _get_handler()(event, context)