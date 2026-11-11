import copy
import json


RULESET_REVISION = "candidate-backward-v1"

PRIMITIVES = {
    "boolean",
    "int",
    "long",
    "float",
    "double",
    "bytes",
    "string",
}

NUMERIC = {"int", "long", "float", "double"}


def _result(validation, compatibility, reason):
    return {
        "validation": validation,
        "compatibility": compatibility,
        "reason": reason,
        "ruleset_revision": RULESET_REVISION,
    }


def _same(first, second):
    def encode(value):
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    return encode(first) == encode(second)


def _flat_type(value):
    nullable = False

    if isinstance(value, list):
        if (
            len(value) != 2
            or value[0] != "null"
            or value[1] == "null"
        ):
            return None

        nullable = True
        value = value[1]

    if isinstance(value, str):
        if value not in PRIMITIVES:
            return None

        return {
            "nullable": nullable,
            "base": value,
            "definition": value,
        }

    if not isinstance(value, dict):
        return None

    base = value.get("type")

    if (
        base not in PRIMITIVES
        or set(value) - {"type", "logicalType", "precision", "scale"}
    ):
        return None

    return {
        "nullable": nullable,
        "base": base,
        "definition": value,
    }


def assess_contracts(
    *,
    candidate,
    baseline,
    validate_contract,
    canonicalise_schema,
):
    candidate = copy.deepcopy(candidate)
    baseline = copy.deepcopy(baseline)

    try:
        validate_contract(candidate)
    except ValueError:
        return _result(
            "INVALID",
            "UNSUPPORTED",
            "INVALID_CANDIDATE",
        )

    try:
        candidate_schema = canonicalise_schema(candidate)
    except ValueError:
        return _result(
            "VALID",
            "UNSUPPORTED",
            "UNSUPPORTED_CANDIDATE_STRUCTURE",
        )

    candidate_fields = {
        field["name"]: field
        for field in candidate["fields"]
    }
    candidate_types = {
        name: _flat_type(field["type"])
        for name, field in candidate_fields.items()
    }

    if any(value is None for value in candidate_types.values()):
        return _result(
            "VALID",
            "UNSUPPORTED",
            "COMPLEX_OR_UNSUPPORTED_TYPE",
        )

    if any(
        field.get("key") not in (None, "primary")
        for field in candidate_fields.values()
    ):
        return _result(
            "VALID",
            "UNSUPPORTED",
            "UNSUPPORTED_KEY_METADATA",
        )

    if baseline is None:
        return _result(
            "VALID",
            "FIRST_CONTRACT",
            "NO_BASELINE",
        )

    try:
        validate_contract(baseline)
        baseline_schema = canonicalise_schema(baseline)
    except ValueError:
        return _result(
            "VALID",
            "UNSUPPORTED",
            "INVALID_OR_UNSUPPORTED_BASELINE",
        )

    baseline_fields = {
        field["name"]: field
        for field in baseline["fields"]
    }
    baseline_types = {
        name: _flat_type(field["type"])
        for name, field in baseline_fields.items()
    }

    if any(value is None for value in baseline_types.values()):
        return _result(
            "VALID",
            "UNSUPPORTED",
            "COMPLEX_OR_UNSUPPORTED_TYPE",
        )

    if any(
        field.get("key") not in (None, "primary")
        for field in baseline_fields.values()
    ):
        return _result(
            "VALID",
            "UNSUPPORTED",
            "UNSUPPORTED_KEY_METADATA",
        )

    candidate_metadata = {
        key: value
        for key, value in candidate.items()
        if key not in {"fields", "contract_version"}
    }
    baseline_metadata = {
        key: value
        for key, value in baseline.items()
        if key not in {"fields", "contract_version"}
    }

    if not _same(candidate_metadata, baseline_metadata):
        return _result(
            "VALID",
            "UNSUPPORTED",
            "CONTRACT_METADATA_CHANGED",
        )

    old_names = list(baseline_fields)
    new_names = list(candidate_fields)

    if set(old_names) - set(new_names):
        return _result(
            "VALID",
            "BREAKING",
            "FIELD_REMOVED_OR_RENAMED",
        )

    for name in old_names:
        old_field = baseline_fields[name]
        new_field = candidate_fields[name]
        old_type = baseline_types[name]
        new_type = candidate_types[name]

        if old_field.get("key") != new_field.get("key"):
            return _result(
                "VALID",
                "BREAKING",
                "PRIMARY_KEY_CHANGED",
            )

        if old_type["nullable"] and not new_type["nullable"]:
            return _result(
                "VALID",
                "BREAKING",
                "NULLABILITY_NARROWED",
            )

        if old_type["base"] != new_type["base"]:
            if {old_type["base"], new_type["base"]} <= NUMERIC:
                return _result(
                    "VALID",
                    "UNSUPPORTED",
                    "NUMERIC_TYPE_CHANGE_REQUIRES_RULE",
                )

            return _result(
                "VALID",
                "BREAKING",
                "INCOMPATIBLE_PRIMITIVE_TYPE",
            )

        if not _same(old_type, new_type):
            return _result(
                "VALID",
                "UNSUPPORTED",
                "LOGICAL_TYPE_OR_NULLABILITY_CHANGED",
            )

        old_annotations = {
            key: value
            for key, value in old_field.items()
            if key not in {"name", "type"}
        }
        new_annotations = {
            key: value
            for key, value in new_field.items()
            if key not in {"name", "type"}
        }

        if not _same(old_annotations, new_annotations):
            return _result(
                "VALID",
                "UNSUPPORTED",
                "FIELD_METADATA_OR_DEFAULT_CHANGED",
            )

    if new_names[:len(old_names)] != old_names:
        return _result(
            "VALID",
            "UNSUPPORTED",
            "FIELD_ORDER_CHANGED",
        )

    additions = new_names[len(old_names):]

    for name in additions:
        field = candidate_fields[name]

        if field.get("key") == "primary":
            return _result(
                "VALID",
                "BREAKING",
                "PRIMARY_KEY_CHANGED",
            )

        if set(field) - {"name", "type", "default"}:
            return _result(
                "VALID",
                "UNSUPPORTED",
                "NEW_FIELD_METADATA_REQUIRES_REVIEW",
            )

        if not candidate_types[name]["nullable"]:
            return _result(
                "VALID",
                "UNSUPPORTED" if "default" in field else "BREAKING",
                "REQUIRED_FIELD_ADDED",
            )

        if "default" not in field or field["default"] is not None:
            return _result(
                "VALID",
                "UNSUPPORTED",
                "NULLABLE_ADDITION_REQUIRES_NULL_DEFAULT",
            )

    if additions:
        return _result(
            "VALID",
            "COMPATIBLE",
            "NULLABLE_FIELDS_APPENDED",
        )

    if _same(candidate_schema, baseline_schema):
        return _result(
            "VALID",
            "NO_CHANGE",
            "UNCHANGED_SUPPORTED_CONTRACT",
        )

    return _result(
        "VALID",
        "UNSUPPORTED",
        "UNCLASSIFIED_CHANGE",
    )