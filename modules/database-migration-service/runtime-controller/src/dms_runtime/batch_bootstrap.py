import os
from collections.abc import Callable, Mapping
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config

from dms_runtime.batch_component_adapters import (
    build_component_factories,
    read_component_targets,
)

from dms_runtime.batch_downstream import BatchDownstream
from dms_runtime.batch_handler import BatchHandler
from dms_runtime.batch_lambda_function import build_batch_entry_point
from dms_runtime.batch_processing import (
    BatchProcessing,
    BatchPublication,
    BatchRawHistory,
    BatchReconciliation,
    ProcessingAdapter,
)
from dms_runtime.cdc_drain import CdcDrain
from dms_runtime.cdc_readiness import CdcReadiness
from dms_runtime.cdc_shutdown_actions import (
    CdcConsumerStop,
    CdcHistoryFinalisation,
)
from dms_runtime.lookup import PinnedRuntimeLookup
from dms_runtime.downstream_context import read_downstream_context
from dms_runtime.ownership import RuntimeContext
from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.lifecycle import IngestionLifecycle
from ingestion_registry.ownership import OwnershipClaim
from ingestion_registry.progress import IngestionProgress
from dms_runtime.downstream_binding_lookup import DownstreamComponentLookup
from dms_runtime.component_binding_action import (
    ACTION as PIN_COMPONENT_ACTION,
    build_component_binding_entry_point,
)


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
        context_reader: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self._component = component
        self._stage_type = stage_type
        self._lookup = lookup
        self._progress = progress
        self._adapter_factory = adapter_factory
        self._reconciliation_required = reconciliation_required
        self._context_reader = context_reader

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
            context_reader=self._context_reader,
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
    reconciliation_factory: (
        Callable[[BatchDownstream, IngestionProgress], BatchReconciliation]
        | None
    ) = None,
    cdc_readiness_factory: (
        Callable[[PinnedRuntimeLookup, IngestionProgress], CdcReadiness] | None
    ) = None,
    cdc_drain_factory: Callable[[IngestionProgress], CdcDrain] | None = None,
    cdc_consumer_stop_factory: (
        Callable[[BatchDownstream, IngestionProgress], CdcConsumerStop] | None
    ) = None,
    cdc_history_finalisation_factory: (
        Callable[[BatchDownstream, IngestionProgress], CdcHistoryFinalisation]
        | None
    ) = None,
) -> Callable[[Any, Any], dict[str, str | int]]:
    context = RuntimeContext(
        environment=_required(environment, "RUNTIME_ENVIRONMENT"),
        account_id=_required(environment, "RUNTIME_ACCOUNT_ID"),
        region=_required(environment, "AWS_REGION"),
        source=_required(environment, "RUNTIME_SOURCE"),
        database_name=_required(environment, "RUNTIME_DATABASE_NAME"),
    )
    reconciliation_setting = _required(
        environment, "BATCH_RECONCILIATION_REQUIRED"
    )
    if reconciliation_setting not in {"true", "false"}:
        raise ValueError("BATCH_RECONCILIATION_REQUIRED must be true or false")
    reconciliation_required = reconciliation_setting == "true"

    targets = read_component_targets(
        environment,
        context=context,
    )
    if targets and adapter_factories is not None:
        raise ValueError(
            "Use configured targets or injected factories, not both"
        )

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
    if targets:
        factories = build_component_factories(
            client=session.client(
                "lambda",
                config=Config(
                    region_name=context.region,
                    connect_timeout=5,
                    read_timeout=45,
                    retries={
                        "mode": "standard",
                        "total_max_attempts": 1,
                    },
                ),
            ),
            targets=targets,
            context=context,
        )
    context_version = environment.get("BATCH_CONTEXT_VERSION", "legacy")
    if context_version not in {"legacy", "1"}:
        raise ValueError(
            "BATCH_CONTEXT_VERSION must be legacy or 1"
        )

    def read_context(ingestion_id: str) -> dict[str, Any]:
        return read_downstream_context(
            registry=registry,
            lookup=lookup,
            ingestion_id=ingestion_id,
        )

    context_reader = read_context if context_version == "1" else None

    def stage(component: str, stage_type: type) -> _ConfiguredBatchStage:
        return _ConfiguredBatchStage(
            component=component,
            stage_type=stage_type,
            lookup=(
                DownstreamComponentLookup(
                    lookup,
                    component=component,
                    registry=registry,
                )
                if context_version == "1"
                else lookup
            ),
            progress=progress,
            adapter_factory=factories.get(component),
            reconciliation_required=reconciliation_required,
            context_reader=context_reader,
        )
    downstream = BatchDownstream(
        resolve_deployment=lookup.resolve,
        read_stage=progress.read_stage,
        context_reader=context_reader,
    )
    reconciliation = None
    if reconciliation_required and reconciliation_factory is not None:
        reconciliation = reconciliation_factory(downstream, progress)

    handler = BatchHandler(
        processing=stage("processing", BatchProcessing),
        raw_history=stage("raw_history", BatchRawHistory),
        publication=stage("publication", BatchPublication),
        lifecycle=IngestionLifecycle(progress),
        reconciliation_required=reconciliation_required,
        reconciliation=reconciliation,
        cdc_readiness=(
            cdc_readiness_factory(lookup, progress)
            if cdc_readiness_factory is not None
            else None
        ),
        cdc_drain=(
            cdc_drain_factory(progress) if cdc_drain_factory is not None else None
        ),
        cdc_consumer_stop=(
            cdc_consumer_stop_factory(downstream, progress)
            if cdc_consumer_stop_factory is not None
            else None
        ),
        cdc_history_finalisation=(
            cdc_history_finalisation_factory(downstream, progress)
            if cdc_history_finalisation_factory is not None
            else None
        ),
    )
    batch_entry = build_batch_entry_point(
        handler=handler,
        registry=registry,
        check_scope=lookup.resolve,
    )

    binding_entry = build_component_binding_entry_point(
        registry=registry,
        lookup=lookup,
    )

    def entry_point(event, context):
        if (
            isinstance(event, dict)
            and event.get("action") == PIN_COMPONENT_ACTION
        ):
            if context_version != "1":
                raise ValueError(
                    "Component pinning requires BATCH_CONTEXT_VERSION=1"
                )

            return binding_entry(event, context)

        return batch_entry(event, context)

    return entry_point

@lru_cache(maxsize=1)
def _get_handler() -> Callable[[Any, Any], dict[str, str | int]]:
    return build_handler(os.environ, session=boto3.session.Session())


def lambda_handler(event: Any, context: Any) -> dict[str, str | int]:
    return _get_handler()(event, context)