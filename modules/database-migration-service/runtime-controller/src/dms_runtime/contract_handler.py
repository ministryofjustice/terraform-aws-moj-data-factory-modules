import json
import re

from dms_runtime.contract_factory import ContractRuntime
from ingestion_registry.identity import TableIdentity
from ingestion_registry.ownership import OwnershipClaim


MAX_REQUEST_BYTES = 16384

REQUIRED_FIELDS = {
    "RESOLVE_CONTRACT": set(),
    "PREPARE_CONTRACT": {
        "request_id",
    },
    "ASSESS_CONTRACT": {
        "reservation_key",
        "preparation_fingerprint",
    },
    "RESUME_CONTRACT": {
        "request_id",
        "contract_version",
        "expected_request_fingerprint",
    },
    "PIN_CONTRACT": {
        "ingestion_id",
        "execution_id",
        "fencing_token",
        "attempt",
    },
}


class ContractRequestError(ValueError):
    """The contract operation request is invalid."""


def _text(value, *, max_bytes):
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value.encode("utf-8")) <= max_bytes
    )


def _fingerprint(value):
    return (
        isinstance(value, str)
        and re.fullmatch(r"[a-f0-9]{64}", value) is not None
    )


def validate_contract_request(event):
    if not isinstance(event, dict):
        raise ContractRequestError(
            "Contract request must be a JSON object"
        )

    if (
        type(event.get("request_version")) is not int
        or event["request_version"] != 1
    ):
        raise ContractRequestError(
            "request_version must be the integer 1"
        )

    operation = event.get("operation")

    if (
        not isinstance(operation, str)
        or operation not in REQUIRED_FIELDS
    ):
        raise ContractRequestError(
            "Unsupported contract operation"
        )

    expected = {
        "request_version",
        "operation",
    } | REQUIRED_FIELDS[operation]

    if set(event) != expected:
        raise ContractRequestError(
            f"{operation} requires exactly: "
            + ", ".join(sorted(expected))
        )

    if operation == "PIN_CONTRACT":
        for field in ("ingestion_id", "execution_id"):
            if not _text(event[field], max_bytes=256):
                raise ContractRequestError(f"Invalid {field}")

        for field in ("fencing_token", "attempt"):
            if type(event[field]) is not int or event[field] < 1:
                raise ContractRequestError(
                    f"{field} must be a positive integer"
                )

    if "request_id" in event:
        if not _text(event["request_id"], max_bytes=256):
            raise ContractRequestError(
                "request_id must contain 1 to 256 UTF-8 bytes "
                "without surrounding whitespace"
            )

    if "reservation_key" in event:
        value = event["reservation_key"]

        if (
            not isinstance(value, str)
            or re.fullmatch(
                r"CONTRACTVERSIONREQUEST#[a-f0-9]{64}",
                value,
            ) is None
        ):
            raise ContractRequestError(
                "reservation_key must identify a contract version request"
            )

    for field in (
        "preparation_fingerprint",
        "expected_request_fingerprint",
    ):
        if field in event and not _fingerprint(event[field]):
            raise ContractRequestError(
                f"{field} must contain 64 lowercase hexadecimal characters"
            )

    if "contract_version" in event:
        value = event["contract_version"]

        if (
            not isinstance(value, str)
            or re.fullmatch(r"v[1-9][0-9]*", value) is None
        ):
            raise ContractRequestError(
                "contract_version must use v1, v2, etc."
            )

    try:
        encoded = json.dumps(
            event,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ContractRequestError(
            "Contract request must contain valid UTF-8 JSON values"
        ) from error

    if len(encoded) > MAX_REQUEST_BYTES:
        raise ContractRequestError(
            "Contract request exceeds 16 KiB"
        )

    return dict(event)


class ContractHandler:
    def __init__(self, runtime: ContractRuntime):
        if not isinstance(runtime, ContractRuntime):
            raise ValueError(
                "runtime must be constructed as ContractRuntime"
            )

        self.runtime = runtime

    def handle(self, event):
        request = validate_contract_request(event)
        coordinator = self.runtime.orchestration
        table = self.runtime.table
        operation = request["operation"]

        if operation == "RESOLVE_CONTRACT":
            return coordinator.resolve(table=table)

        if operation == "PREPARE_CONTRACT":
            return coordinator.prepare(
                table=table,
                request_id=request["request_id"],
            )

        if operation == "ASSESS_CONTRACT":
            return coordinator.assess(
                table=table,
                reservation_key=request["reservation_key"],
                preparation_fingerprint=request[
                    "preparation_fingerprint"
                ],
            )

        if operation == "PIN_CONTRACT":
            pinning = self.runtime.pinning
            ingestion_id = request["ingestion_id"]
            ingestion = pinning.registry._read(
                f"INGESTION#{ingestion_id}",
                "META",
            )

            if (
                ingestion is None
                or TableIdentity(**ingestion["table_identity"]) != table
            ):
                raise ContractRequestError(
                    "Ingestion is outside the configured table"
                )

            claim = OwnershipClaim(
                ingestion_id=ingestion_id,
                execution_id=request["execution_id"],
                fencing_token=request["fencing_token"],
                attempt=request["attempt"],
                lease_expires_at=0,
                recovery_required=False,
            )
            return pinning.pin(
                claim,
                update_id="select-contract",
                expected_revision=0,
            )

        return coordinator.advance(
            table=table,
            request_id=request["request_id"],
            contract_version=request["contract_version"],
            expected_request_fingerprint=request[
                "expected_request_fingerprint"
            ],
        )