import json
import re
import urllib.parse
from dataclasses import asdict
import copy

from ingestion_registry.contract_registration_authorisation import (
    authorisation_fields,
    authorise_registration,
)

from ingestion_registry.contract_verification import (
    ContractNotRegistered,
)
from ingestion_registry.identity import TableIdentity


class ContractRegistrationBlocked(ValueError):
    """Registration does not match the approved promoted contract."""


class ContractRegistrationFailed(RuntimeError):
    """The registration invocation did not confirm successful completion."""


def _unique_object(pairs):
    result = {}

    for key, value in pairs:
        if key in result:
            raise ContractRegistrationFailed(
                "Registration response contains duplicate JSON keys"
            )
        result[key] = value

    return result


def _reject_constant(value):
    raise ContractRegistrationFailed(
        f"Registration response contains invalid JSON constant: {value}"
    )


class ContractRegistrar:
    def __init__(
        self,
        *,
        promoter,
        approval_gate,
        verifier,
        lambda_client,
        registration_function_arn: str,
        policy_authorisation=None,
    ):
        if (
            not isinstance(registration_function_arn, str)
            or re.fullmatch(
                r"arn:aws:lambda:[a-z0-9-]+:[0-9]{12}:"
                r"function:[A-Za-z0-9_-]+:[1-9][0-9]*",
                registration_function_arn,
            ) is None
        ):
            raise ValueError(
                "registration_function_arn must reference "
                "a published numeric Lambda version"
            )

        self.promoter = promoter
        self.approval_gate = approval_gate
        self.policy_authorisation = policy_authorisation
        self.verifier = verifier
        self.lambda_client = lambda_client
        self.registration_function_arn = registration_function_arn

    def _check_authorisation(
        self,
        *,
        table,
        request_id,
        promoted,
    ):
        current = authorise_registration(
            approval_gate=self.approval_gate,
            policy_authorisation=self.policy_authorisation,
            table=table,
            request_id=request_id,
            contract_version=promoted["contract_version"],
        )

        expected = authorisation_fields(current)

        if any(
            promoted.get(field) != value
            for field, value in expected.items()
        ):
            raise ContractRegistrationBlocked(
                "Promotion no longer matches the current authorisation"
            )

        if current["kind"] == "HUMAN":
            if "authorisation" in promoted:
                raise ContractRegistrationBlocked(
                    "Promotion has a conflicting authorisation type"
                )
        elif (
            "approval_reference" in promoted
            or "approval_revision" in promoted
        ):
            raise ContractRegistrationBlocked(
                "Policy promotion must not contain human approval fields"
            )

    @staticmethod
    def _check_registered(contract, promoted):
        if (
            contract.contract_version != promoted["contract_version"]
            or contract.contract_fingerprint
            != promoted["contract_fingerprint"]
            or asdict(contract.object_reference)
            != promoted["object_reference"]
        ):
            raise ContractRegistrationBlocked(
                "Registered contract does not match "
                "the exact promoted object and content"
            )

    def _invoke(self, promoted):
        reference = promoted["object_reference"]
        event = {
            "Records": [
                {
                    "s3": {
                        "bucket": {
                            "name": reference["bucket_name"],
                        },
                        "object": {
                            "key": urllib.parse.quote_plus(
                                reference["object_key"],
                                safe="",
                            ),
                            "versionId": reference["version_id"],
                        },
                    }
                }
            ]
        }

        response = self.lambda_client.invoke(
            FunctionName=self.registration_function_arn,
            InvocationType="RequestResponse",
            Payload=json.dumps(
                event,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8"),
        )

        payload = response.get("Payload")

        try:
            if (
                response.get("StatusCode") != 200
                or response.get("FunctionError") is not None
            ):
                raise ContractRegistrationFailed(
                    "Registration Lambda invocation failed"
                )

            expected_version = self.registration_function_arn.rsplit(
                ":", 1
            )[1]

            if response.get("ExecutedVersion") != expected_version:
                raise ContractRegistrationFailed(
                    "Registration executed a different Lambda version"
                )

            if payload is None:
                raise ContractRegistrationFailed(
                    "Registration returned no payload"
                )

            raw = payload.read(65537)
        finally:
            if payload is not None:
                payload.close()

        if len(raw) > 65536:
            raise ContractRegistrationFailed(
                "Registration response exceeds 64 KiB"
            )

        try:
            result = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContractRegistrationFailed(
                "Registration returned invalid UTF-8 JSON"
            ) from error

        if (
            not isinstance(result, dict)
            or type(result.get("statusCode")) is not int
            or result["statusCode"] != 200
            or not isinstance(result.get("results"), list)
            or len(result["results"]) != 1
            or not isinstance(result["results"][0], dict)
            or result["results"][0].get("status")
            not in {"REGISTERED", "ALREADY_REGISTERED"}
        ):
            raise ContractRegistrationFailed(
                "Registration did not return a completed result"
            )


    def register(
        self,
        *,
        table: TableIdentity,
        request_id: str,
        contract_version: str,
    ) -> dict:
        promoted = self.promoter.promote(
            table=table,
            request_id=request_id,
            contract_version=contract_version,
        )

        self._check_authorisation(
            table=table,
            request_id=request_id,
            promoted=promoted,
        )

        try:
            contract = self.verifier.verify(
                identity=table,
                contract_version=contract_version,
            )
        except ContractNotRegistered:
            self._invoke(promoted)

            contract = self.verifier.verify(
                identity=table,
                contract_version=contract_version,
            )

        self._check_registered(contract, promoted)

        self._check_authorisation(
            table=table,
            request_id=request_id,
            promoted=promoted,
        )

        result = {
            "status": "REGISTRATION_CONFIRMED",
            "request_key": promoted["request_key"],
            "request_fingerprint": promoted["request_fingerprint"],
            "candidate_reference": copy.deepcopy(
                promoted["candidate_reference"]
            ),
            "registered_contract": asdict(contract),
        }

        if "authorisation" in promoted:
            result["authorisation"] = copy.deepcopy(
                promoted["authorisation"]
            )
        else:
            result["approval_revision"] = promoted["approval_revision"]
            result["approval_reference"] = copy.deepcopy(
                promoted["approval_reference"]
            )

        return result