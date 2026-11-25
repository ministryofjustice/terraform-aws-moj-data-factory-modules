import copy
import json

from ingestion_config.component_bindings import resolve_component_bindings
from ingestion_registry.downstream_bindings import (
    PROFILES,
    DownstreamBindingConflict,
    DownstreamBindings,
)
from ingestion_registry.references import S3ObjectReference

from dms_runtime.processing_inputs import read_pinned_input


MAX_CATALOG_BYTES = 256 * 1024


def _unique_object(pairs):
    result = {}

    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate catalog key: {key}")

        result[key] = value

    return result


def _reject_constant(value):
    raise ValueError(f"Invalid catalog constant: {value}")


class DownstreamComponentLookup:
    def __init__(
        self,
        base_lookup,
        *,
        component,
        registry=None,
        clock=None,
    ):
        if component not in PROFILES:
            raise ValueError("Unsupported downstream component")

        self.base = base_lookup
        self.component = component
        self.registry = (
            registry
            if registry is not None
            else base_lookup.registry
        )
        self.bindings = DownstreamBindings(
            self.registry,
            clock=clock,
        )

    def _target(self, base, reference):
        if not isinstance(reference, S3ObjectReference):
            raise ValueError(
                "A versioned component catalog reference is required"
            )

        if (
            reference.bucket_name != self.base.allowed_bucket
            or not reference.object_key.startswith(
                self.base.allowed_prefix
            )
        ):
            raise DownstreamBindingConflict(
                "Component catalog is outside "
                "the published document boundary"
            )

        raw = read_pinned_input(
            self.base.document_client,
            reference,
            owner=self.base.expected_bucket_owner,
            limit=MAX_CATALOG_BYTES,
            label="Downstream component catalog",
            size_error=(
                "Downstream component catalog exceeds 256 KiB"
            ),
        )

        try:
            catalog = json.loads(
                raw,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )

        except (
            TypeError,
            ValueError,
            UnicodeError,
            RecursionError,
        ) as error:
            raise DownstreamBindingConflict(
                "Invalid downstream component catalog JSON"
            ) from error

        if (
            not isinstance(catalog, dict)
            or type(catalog.get("catalog_version")) is not int
            or catalog["catalog_version"] != 1
        ):
            raise DownstreamBindingConflict(
                "Component catalog_version must be the integer 1"
            )

        return resolve_component_bindings(
            base,
            catalog,
            required_components=[self.component],
        )[self.component]

    def pin(self, claim, *, catalog_reference):
        base = self.base.resolve(claim.ingestion_id)
        target = self._target(base, catalog_reference)

        return self.bindings.pin(
            claim,
            component=self.component,
            base_binding=base,
            catalog_reference=catalog_reference,
            target=target,
        )

    def resolve(self, ingestion_id):
        base = self.base.resolve(ingestion_id)

        if self.component in base.get("component_bindings", {}):
            return base

        record = self.bindings.read(
            ingestion_id=ingestion_id,
            component=self.component,
            base_binding=base,
        )

        if record is None:
            return base

        payload = record["payload"]
        reference = S3ObjectReference(
            **payload["catalog_reference"]
        )
        target = self._target(base, reference)

        if target != payload["target"]:
            raise DownstreamBindingConflict(
                "Pinned target differs from "
                "its versioned component catalog"
            )

        result = copy.deepcopy(base)

        result["component_bindings"] = {
            **base.get("component_bindings", {}),
            self.component: target,
        }
        result["downstream_binding"] = {
            "component": self.component,
            "binding_sha256": record["binding_sha256"],
            "catalog_reference": dict(
                payload["catalog_reference"]
            ),
        }

        return result