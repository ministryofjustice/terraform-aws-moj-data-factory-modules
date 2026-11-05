import json
import re
from dataclasses import asdict

from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference


class ContractGenerationError(ValueError):
    """Generation did not return a verified selected-table candidate."""


def _unique_object(pairs):
    result = {}

    for key, value in pairs:
        if key in result:
            raise ContractGenerationError(
                f"Generation response contains duplicate JSON key: {key}"
            )
        result[key] = value

    return result


def _reject_constant(value):
    raise ContractGenerationError(
        f"Generation response contains invalid JSON constant: {value}"
    )


class ContractGenerator:
    def __init__(
        self,
        *,
        registry,
        candidate_reader,
        lambda_client,
        generation_function_arn: str,
    ):
        match = (
            re.fullmatch(
                r"arn:aws:lambda:([a-z0-9-]+):([0-9]{12}):"
                r"function:([A-Za-z0-9_-]+):([1-9][0-9]*)",
                generation_function_arn,
            )
            if isinstance(generation_function_arn, str)
            else None
        )

        if match is None:
            raise ValueError(
                "generation_function_arn must reference a "
                "published numeric Lambda version"
            )

        self.registry = registry
        self.candidate_reader = candidate_reader
        self.lambda_client = lambda_client
        self.function_arn = generation_function_arn
        self.function_version = match.group(4)

    def generate(
        self,
        *,
        table: TableIdentity,
        reservation_key: str,
        request_fingerprint: str,
    ) -> dict:
        if not isinstance(table, TableIdentity):
            raise ValueError("table must be a TableIdentity")

        if (
            not isinstance(reservation_key, str)
            or re.fullmatch(
                r"CONTRACTVERSIONREQUEST#[a-f0-9]{64}",
                reservation_key,
            ) is None
        ):
            raise ValueError("Invalid contract version reservation key")

        if (
            not isinstance(request_fingerprint, str)
            or re.fullmatch(
                r"[a-f0-9]{64}",
                request_fingerprint,
            ) is None
        ):
            raise ValueError(
                "request_fingerprint must contain "
                "64 lowercase hexadecimal characters"
            )

        reservation = self.registry._read(reservation_key, "META")

        if (
            not isinstance(reservation, dict)
            or reservation.get("record_type")
            != "CONTRACT_VERSION_RESERVATION"
            or reservation.get("pk") != reservation_key
            or reservation.get("sk") != "META"
            or reservation.get("table_identity") != asdict(table)
            or reservation.get("request_fingerprint")
            != request_fingerprint
        ):
            raise ContractGenerationError(
                "Version reservation does not match the generation request"
            )

        request_id = reservation.get("request_id")
        version = reservation.get("contract_version")

        if (
            not isinstance(request_id, str)
            or not request_id
            or request_id != request_id.strip()
            or len(request_id.encode("utf-8")) > 256
            or not isinstance(version, str)
            or re.fullmatch(r"v[1-9][0-9]*", version) is None
        ):
            raise ContractGenerationError(
                "Version reservation contains an invalid request or version"
            )

        identity = {
            "source": table.source,
            "database_name": table.database_name,
            "schema_name": table.schema_name,
            "table_name": table.table_name,
            "contract_version": version,
        }

        request = {
            "operation": "GENERATE_CANDIDATE",
            "request_id": request_id,
            **identity,
            "version_reservation": {
                "request_key": reservation_key,
                "request_fingerprint": request_fingerprint,
            },
        }

        response = self.lambda_client.invoke(
            FunctionName=self.function_arn,
            InvocationType="RequestResponse",
            Payload=json.dumps(
                request,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8"),
        )

        result = self._read_response(response)

        if result.get("status") != "SUCCEEDED":
            raise ContractGenerationError(
                "Generation handler did not report successful completion"
            )

        contracts = result.get("contracts")

        if (
            not isinstance(contracts, list)
            or len(contracts) != 1
            or not isinstance(contracts[0], dict)
        ):
            raise ContractGenerationError(
                "Generation must return exactly one selected-table candidate"
            )

        candidate = contracts[0]

        if any(
            candidate.get(field) != value
            for field, value in identity.items()
        ):
            raise ContractGenerationError(
                "Generated candidate identity does not match the request"
            )

        if candidate.get("status") != "CANDIDATE_STORED":
            raise ContractGenerationError(
                "Generation did not confirm candidate storage"
            )

        reference_data = candidate.get("candidate_reference")

        if (
            not isinstance(reference_data, dict)
            or set(reference_data)
            != {"bucket_name", "object_key", "version_id", "sha256"}
        ):
            raise ContractGenerationError(
                "Generation returned an invalid candidate reference"
            )

        try:
            reference = S3ObjectReference(**reference_data)
        except ValueError as error:
            raise ContractGenerationError(
                "Generation returned an invalid candidate reference"
            ) from error

        verified = self.candidate_reader.read(
            table=table,
            contract_version=version,
            reference=reference,
        )

        if (
            candidate.get("contract_fingerprint")
            != verified.contract_fingerprint
        ):
            raise ContractGenerationError(
                "Generation fingerprint does not match verified candidate content"
            )

        if self.registry._read(reservation_key, "META") != reservation:
            raise ContractGenerationError(
                "Version reservation changed during generation"
            )

        return {
            "status": "CANDIDATE_VERIFIED",
            "request_id": request_id,
            "reservation_key": reservation_key,
            "request_fingerprint": request_fingerprint,
            "generation_function_arn": self.function_arn,
            **identity,
            "candidate_reference": asdict(verified.reference),
            "contract_fingerprint": verified.contract_fingerprint,
        }

    def _read_response(self, response):
        payload = response.get("Payload")

        if payload is None:
            raise ContractGenerationError(
                "Generation invocation returned no payload"
            )

        try:
            if (
                type(response.get("StatusCode")) is not int
                or response["StatusCode"] != 200
                or "FunctionError" in response
                or response.get("ExecutedVersion")
                != self.function_version
            ):
                raise ContractGenerationError(
                    "Generation invocation failed or used an unexpected version"
                )

            raw = payload.read(65537)
        finally:
            payload.close()

        if len(raw) > 65536:
            raise ContractGenerationError(
                "Generation response exceeds the size limit"
            )

        try:
            result = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContractGenerationError(
                "Generation response is not valid UTF-8 JSON"
            ) from error

        if not isinstance(result, dict):
            raise ContractGenerationError(
                "Generation response must be a JSON object"
            )

        return result