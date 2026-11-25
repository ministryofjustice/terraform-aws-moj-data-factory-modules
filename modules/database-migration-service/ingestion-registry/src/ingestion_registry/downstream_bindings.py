import copy
import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal

from ingestion_registry.identity import TableIdentity
from ingestion_registry.ownership import OwnershipClaim
from ingestion_registry.progress import IngestionProgress, ProgressConflict
from ingestion_registry.references import S3ObjectReference


PROFILES = {
    "processing": "processing_profile",
    "raw_history": "history_profile",
    "publication": "publication_profile",
}

STAGES = {
    "processing": (
        "PROCESSING",
        "CDC_CONSUMER",
        "CDC_CONSUMER_STOPPED",
    ),
    "raw_history": (
        "HISTORY_BASELINE",
        "HISTORY_CDC",
        "HISTORY_FINALIZED",
    ),
    "publication": ("PUBLICATION",),
}


class DownstreamBindingConflict(ProgressConflict):
    """A component target conflicts with its binding or input evidence."""


def _json_value(value):
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)

    raise TypeError("Downstream bindings require integral JSON values")


def _reject_float(value):
    raise ValueError(
        "Downstream bindings must not contain floating-point values"
    )


def _json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_json_value,
    )


def _plain(value):
    return json.loads(
        _json(value),
        parse_float=_reject_float,
    )


def _sha(value):
    return hashlib.sha256(
        _json(value).encode("utf-8")
    ).hexdigest()


class DownstreamBindings(IngestionProgress):
    def _payload(
        self,
        ingestion_id,
        component,
        base,
        catalog_reference,
        target,
    ):
        if component not in PROFILES:
            raise ValueError("Unsupported downstream component")

        if (
            not isinstance(ingestion_id, str)
            or not ingestion_id
            or ingestion_id != ingestion_id.strip()
            or len(ingestion_id.encode("utf-8")) > 256
        ):
            raise ValueError("Invalid ingestion_id")

        if not isinstance(catalog_reference, S3ObjectReference):
            raise ValueError(
                "A versioned component catalog reference is required"
            )

        base = _plain(base)
        pk = f"INGESTION#{ingestion_id}"

        metadata = self.registry._read(pk, "META")
        pinned = self.registry._read(pk, "BINDING")

        if metadata is None or pinned is None:
            raise DownstreamBindingConflict(
                "Ingestion metadata and extraction binding are required"
            )

        fingerprint = _sha(base)

        if (
            pinned.get("binding_fingerprint") != fingerprint
            or _sha(_plain(pinned.get("binding"))) != fingerprint
        ):
            raise DownstreamBindingConflict(
                "Base binding differs from the original extraction binding"
            )

        identity = asdict(
            TableIdentity(**metadata["table_identity"])
        )

        if (
            any(
                base[field] != identity[field]
                for field in (
                    "environment",
                    "source",
                    "database_name",
                )
            )
            or any(
                base["table"][field] != identity[field]
                for field in (
                    "schema_name",
                    "table_name",
                )
            )
            or any(
                base[field] != metadata[field]
                for field in (
                    "configuration_revision",
                    "artifact_revision",
                    "extraction_generation",
                )
            )
        ):
            raise DownstreamBindingConflict(
                "Base binding differs from ingestion metadata"
            )

        version = metadata["ingestion_version"]

        if (
            isinstance(version, bool)
            or not isinstance(version, (int, Decimal))
            or version != int(version)
            or version < 1
        ):
            raise DownstreamBindingConflict(
                "Invalid Registry ingestion version"
            )

        if metadata["operation"] not in {
            "INITIAL_INGESTION",
            "REINGESTION",
        }:
            raise DownstreamBindingConflict(
                "Unsupported ingestion operation"
            )

        original = base.get("component_bindings", {})

        if not isinstance(original, dict) or component in original:
            raise DownstreamBindingConflict(
                "The original binding already owns this component target"
            )

        expected_profile = base["table"].get(PROFILES[component])
        arn_prefix = (
            f"arn:aws:lambda:{base['region']}:"
            f"{base['account_id']}:function:"
        )

        if (
            not isinstance(target, dict)
            or set(target) != {"profile", "kind", "arn"}
            or not isinstance(expected_profile, str)
            or not expected_profile
            or target["profile"] != expected_profile
            or target["kind"] != "lambda"
            or not isinstance(target["arn"], str)
            or re.fullmatch(
                re.escape(arn_prefix)
                + r"[A-Za-z0-9_-]{1,64}:[1-9][0-9]*",
                target["arn"],
            ) is None
        ):
            raise DownstreamBindingConflict(
                "Target must match the pinned profile "
                "and a numeric Lambda version"
            )

        results = {}

        for stage, kind in (
            ("CONTRACT", "registered-contract"),
            ("EXTRACTION", "raw-batch"),
        ):
            record = self.registry._read(pk, f"STAGE#{stage}")
            reference = (record or {}).get("result_reference")

            if (
                record is None
                or record.get("ingestion_id") != ingestion_id
                or record.get("status") != "SUCCEEDED"
                or not isinstance(reference, dict)
                or reference.get("kind") != kind
                or any(
                    not isinstance(key, str)
                    or not isinstance(value, str)
                    or not key
                    or not value
                    for key, value in reference.items()
                )
            ):
                raise DownstreamBindingConflict(
                    f"{stage} completion is required "
                    "before pinning a target"
                )

            results[stage] = copy.deepcopy(reference)

        S3ObjectReference(
            **{
                field: results["EXTRACTION"][field]
                for field in (
                    "bucket_name",
                    "object_key",
                    "version_id",
                    "sha256",
                )
            }
        )

        payload = {
            "binding_version": 1,
            "ingestion_id": ingestion_id,
            "component": component,
            "ingestion_version": int(version),
            "operation": metadata["operation"],
            "table_identity": identity,
            "base_binding_sha256": fingerprint,
            "configuration_revision": metadata["configuration_revision"],
            "artifact_revision": metadata["artifact_revision"],
            "extraction_generation": metadata["extraction_generation"],
            "catalog_reference": asdict(catalog_reference),
            "target": _plain(target),
            "contract_result_sha256": _sha(results["CONTRACT"]),
            "extraction_result_sha256": _sha(results["EXTRACTION"]),
        }

        if len(_json(payload).encode("utf-8")) > 16384:
            raise ValueError(
                "Downstream target binding exceeds 16 KiB"
            )

        return payload, results

    def read(
        self,
        *,
        ingestion_id,
        component,
        base_binding,
    ):
        if component not in PROFILES:
            raise ValueError("Unsupported downstream component")

        record = self.registry._read(
            f"INGESTION#{ingestion_id}",
            f"DOWNSTREAM_BINDING#{component}",
        )

        if record is None:
            return None

        try:
            stored = _plain(record["payload"])

            expected, _ = self._payload(
                ingestion_id,
                component,
                base_binding,
                S3ObjectReference(**stored["catalog_reference"]),
                stored["target"],
            )

            valid = (
                record["record_type"] == "DOWNSTREAM_COMPONENT_BINDING"
                and stored == expected
                and record["binding_sha256"] == _sha(expected)
            )

        except (KeyError, TypeError, ValueError) as error:
            raise DownstreamBindingConflict(
                "Invalid downstream target binding"
            ) from error

        if not valid:
            raise DownstreamBindingConflict(
                "Downstream target binding or its evidence has changed"
            )

        result = copy.deepcopy(record)
        result["payload"] = stored
        return result

    def _pin_checks(self, claim, payload, results, now):
        actions = self._guards(claim, now)

        metadata = actions[1]["ConditionCheck"]
        metadata["ConditionExpression"] += (
            " AND ingestion_version = :plan_version"
            " AND table_identity = :plan_identity"
            " AND configuration_revision = :plan_config"
            " AND artifact_revision = :plan_artifact"
            " AND extraction_generation = :plan_generation"
            " AND #plan_operation = :plan_operation"
        )
        metadata["ExpressionAttributeNames"]["#plan_operation"] = (
            "operation"
        )
        metadata["ExpressionAttributeValues"].update(
            self.registry._encode(
                {
                    ":plan_version": payload["ingestion_version"],
                    ":plan_identity": payload["table_identity"],
                    ":plan_config": payload["configuration_revision"],
                    ":plan_artifact": payload["artifact_revision"],
                    ":plan_generation": payload["extraction_generation"],
                    ":plan_operation": payload["operation"],
                }
            )
        )

        pk = f"INGESTION#{claim.ingestion_id}"

        def check(
            partition,
            sk,
            expression,
            values=None,
            names=None,
        ):
            item = {
                "TableName": self.registry.table_name,
                "Key": self.registry._encode(
                    {"pk": partition, "sk": sk}
                ),
                "ConditionExpression": expression,
            }

            if values is not None:
                item["ExpressionAttributeValues"] = (
                    self.registry._encode(values)
                )

            if names is not None:
                item["ExpressionAttributeNames"] = names

            return {"ConditionCheck": item}

        actions.extend(
            [
                check(
                    pk,
                    "BINDING",
                    "binding_fingerprint = :base",
                    {":base": payload["base_binding_sha256"]},
                ),
                check(
                    TableIdentity(
                        **payload["table_identity"]
                    ).partition_key,
                    "HEAD",
                    "active_ingestion_id = :ingestion",
                    {":ingestion": claim.ingestion_id},
                ),
            ]
        )

        for stage, reference in results.items():
            actions.append(
                check(
                    pk,
                    f"STAGE#{stage}",
                    "#status = :done"
                    " AND result_reference = :reference"
                    " AND ingestion_id = :ingestion",
                    {
                        ":done": "SUCCEEDED",
                        ":reference": reference,
                        ":ingestion": claim.ingestion_id,
                    },
                    {"#status": "status"},
                )
            )

        return actions, check

    def pin(
        self,
        claim,
        *,
        component,
        base_binding,
        catalog_reference,
        target,
    ):
        if not isinstance(claim, OwnershipClaim):
            raise ValueError(
                "An ingestion ownership claim is required"
            )

        payload, results = self._payload(
            claim.ingestion_id,
            component,
            base_binding,
            catalog_reference,
            target,
        )

        existing = self.read(
            ingestion_id=claim.ingestion_id,
            component=component,
            base_binding=base_binding,
        )
        fingerprint = _sha(payload)

        if (
            existing is not None
            and existing["binding_sha256"] != fingerprint
        ):
            raise DownstreamBindingConflict(
                "This component already has a different immutable target"
            )

        now = self.clock()

        if type(now) is not int or now < 0:
            raise ValueError(
                "Registry clock must return non-negative epoch seconds"
            )

        pk = f"INGESTION#{claim.ingestion_id}"
        sk = f"DOWNSTREAM_BINDING#{component}"

        record = existing or {
            "pk": pk,
            "sk": sk,
            "record_type": "DOWNSTREAM_COMPONENT_BINDING",
            "payload": payload,
            "binding_sha256": fingerprint,
            "created_at": datetime.fromtimestamp(
                now,
                tz=timezone.utc,
            ).isoformat(),
            "created_by_execution_id": claim.execution_id,
            "created_by_attempt": claim.attempt,
            "created_by_fencing_token": claim.fencing_token,
        }

        actions, check = self._pin_checks(
            claim,
            payload,
            results,
            now,
        )

        if existing is None:
            for stage in STAGES[component]:
                actions.append(
                    check(
                        pk,
                        f"STAGE#{stage}",
                        "attribute_not_exists(pk)",
                    )
                )

            actions.append(self.registry._put_new(record))

        else:
            actions.append(
                check(
                    pk,
                    sk,
                    "binding_sha256 = :fingerprint",
                    {":fingerprint": fingerprint},
                )
            )

        try:
            self._commit(actions)

        except ProgressConflict as error:
            current = self.read(
                ingestion_id=claim.ingestion_id,
                component=component,
                base_binding=base_binding,
            )

            if (
                current is None
                or current["binding_sha256"] != fingerprint
            ):
                raise DownstreamBindingConflict(
                    "Target pin raced with progress or another target"
                ) from error

            actions, check = self._pin_checks(
                claim,
                payload,
                results,
                now,
            )
            actions.append(
                check(
                    pk,
                    sk,
                    "binding_sha256 = :fingerprint",
                    {":fingerprint": fingerprint},
                )
            )

            self._commit(actions)
            record = current

        return copy.deepcopy(record)