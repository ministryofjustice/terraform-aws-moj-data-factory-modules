import json
import re

from ingestion_registry.references import S3ObjectReference


class ContractValidationFailed(RuntimeError):
    """Validation invocation did not return assessment evidence."""


def _unique_object(pairs):
    result = {}

    for key, value in pairs:
        if key in result:
            raise ContractValidationFailed(
                "Validation response contains duplicate JSON keys"
            )
        result[key] = value

    return result


def _reject_constant(value):
    raise ContractValidationFailed(
        f"Validation response contains an invalid JSON constant: {value}"
    )


class ContractValidator:
    def __init__(
        self,
        *,
        assessment_requests,
        lambda_client,
        validation_function_arn: str,
    ):
        if (
            not isinstance(validation_function_arn, str)
            or re.fullmatch(
                r"arn:aws:lambda:[a-z0-9-]+:[0-9]{12}:"
                r"function:[A-Za-z0-9_-]+:[1-9][0-9]*",
                validation_function_arn,
            ) is None
        ):
            raise ValueError(
                "validation_function_arn must reference "
                "a published numeric Lambda version"
            )

        self.assessment_requests = assessment_requests
        self.lambda_client = lambda_client
        self.validation_function_arn = validation_function_arn

    def assess(
        self,
        *,
        table,
        reservation_key: str,
        preparation_fingerprint: str,
    ) -> dict:
        arguments = {
            "table": table,
            "reservation_key": reservation_key,
            "preparation_fingerprint": preparation_fingerprint,
        }

        recorded = self.assessment_requests.prepare(**arguments)
        request = recorded["request"]

        payload = json.dumps(
            {
                "operation": "ASSESS_CONTRACT",
                "request": request,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=_encode_dynamodb_number,
        ).encode("utf-8")

        if len(payload) > 65536:
            raise ContractValidationFailed(
                "Validation invocation request exceeds 64 KiB"
            )

        response = self.lambda_client.invoke(
            FunctionName=self.validation_function_arn,
            InvocationType="RequestResponse",
            Payload=payload,
        )

        result = self._read_response(response)

        if (
            not isinstance(result, dict)
            or result.get("status") != "ASSESSMENT_COMPLETED"
            or result.get("request_id") != request["request_id"]
        ):
            raise ContractValidationFailed(
                "Validation did not confirm the selected assessment request"
            )

        try:
            reference = S3ObjectReference(
                **result["evidence_reference"]
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ContractValidationFailed(
                "Validation returned an invalid evidence reference"
            ) from error

        latest = self.assessment_requests.read(**arguments)

        if latest != recorded:
            raise ContractValidationFailed(
                "Recorded assessment request changed during validation"
            )

        return {
            "status": "ASSESSMENT_EVIDENCE_RETURNED",
            "request_id": request["request_id"],
            "evidence_reference": reference,
            "validation_function_arn": self.validation_function_arn,
        }

    def _read_response(self, response):
        stream = response.get("Payload")

        try:
            if (
                response.get("StatusCode") != 200
                or response.get("FunctionError") is not None
            ):
                raise ContractValidationFailed(
                    "Validation Lambda invocation failed"
                )

            expected_version = self.validation_function_arn.rsplit(
                ":", 1
            )[1]

            if response.get("ExecutedVersion") != expected_version:
                raise ContractValidationFailed(
                    "Validation executed a different Lambda version"
                )

            if stream is None:
                raise ContractValidationFailed(
                    "Validation returned no payload"
                )

            raw = stream.read(65537)
        finally:
            if stream is not None:
                stream.close()

        if len(raw) > 65536:
            raise ContractValidationFailed(
                "Validation response exceeds 64 KiB"
            )

        try:
            return json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContractValidationFailed(
                "Validation returned invalid UTF-8 JSON"
            ) from error


def _encode_dynamodb_number(value):
    from decimal import Decimal

    if isinstance(value, Decimal) and value.is_finite():
        if value == value.to_integral_value():
            return int(value)

    raise TypeError(
        "Assessment requests must contain JSON values "
        "and integral DynamoDB numbers"
    )