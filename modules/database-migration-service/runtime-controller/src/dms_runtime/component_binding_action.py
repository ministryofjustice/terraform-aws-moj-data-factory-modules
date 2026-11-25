import json

from ingestion_registry.downstream_bindings import PROFILES
from ingestion_registry.references import S3ObjectReference

from dms_runtime.batch_lambda_function import build_batch_entry_point
from dms_runtime.downstream_binding_lookup import DownstreamComponentLookup


ACTION = "pin_downstream_component"
MAX_REQUEST_BYTES = 16384


class _PinHandler:
    def __init__(
        self,
        registry,
        lookup,
        component,
        reference,
        clock,
    ):
        self.registry = registry
        self.lookup = lookup
        self.component = component
        self.reference = reference
        self.clock = clock

    def handle(self, request, *, claim):
        record = DownstreamComponentLookup(
            self.lookup,
            component=self.component,
            registry=self.registry,
            clock=self.clock,
        ).pin(
            claim,
            catalog_reference=self.reference,
        )

        return {
            "action": ACTION,
            "ingestion_id": claim.ingestion_id,
            "component": self.component,
            "status": "PINNED",
            "binding_sha256": record["binding_sha256"],
            "target_arn": record["payload"]["target"]["arn"],
        }


def build_component_binding_entry_point(
    *,
    registry,
    lookup,
    clock=None,
):
    def entry_point(event, context):
        if not isinstance(event, dict) or set(event) != {
            "request_version",
            "action",
            "ingestion_id",
            "ingestion_owner",
            "component",
            "catalog_reference",
        }:
            raise ValueError(
                "Invalid component binding request fields"
            )

        try:
            encoded = json.dumps(
                event,
                allow_nan=False,
            ).encode("utf-8")

        except (
            TypeError,
            ValueError,
            RecursionError,
        ) as error:
            raise ValueError(
                "Component binding request must be valid JSON"
            ) from error

        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError(
                "Component binding request must not exceed 16 KiB"
            )

        if event["action"] != ACTION:
            raise ValueError(
                "Expected pin_downstream_component"
            )

        component = event["component"]

        if (
            not isinstance(component, str)
            or component not in PROFILES
        ):
            raise ValueError(
                "Unsupported downstream component"
            )

        value = event["catalog_reference"]

        if not isinstance(value, dict) or set(value) != {
            "bucket_name",
            "object_key",
            "version_id",
            "sha256",
        }:
            raise ValueError(
                "An explicit versioned catalog reference is required"
            )

        reference = S3ObjectReference(**value)

        owned_entry = build_batch_entry_point(
            handler=_PinHandler(
                registry,
                lookup,
                component,
                reference,
                clock,
            ),
            registry=registry,
            clock=clock,
            check_scope=lookup.resolve,
        )

        return owned_entry(
            {
                field: event[field]
                for field in (
                    "request_version",
                    "action",
                    "ingestion_id",
                    "ingestion_owner",
                )
            },
            context,
        )

    return entry_point