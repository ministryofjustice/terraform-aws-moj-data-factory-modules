import copy

from botocore.exceptions import ClientError
from ingestion_registry.contract_authorisation import human_approval_check

from ingestion_registry.contract_registration import (
    ContractRegistrationBlocked,
)


class ContractRegistrationCompletion:
    def __init__(self, *, registrar, approval_gate):
        self.registrar = registrar
        self.approval_gate = approval_gate
        self.registry = approval_gate.lifecycle.registry

    def complete(
        self,
        *,
        table,
        request_id: str,
        contract_version: str,
    ) -> dict:
        result = self.registrar.register(
            table=table,
            request_id=request_id,
            contract_version=contract_version,
        )

        current = self.approval_gate.authorise_registration(
            table=table,
            request_id=request_id,
        )

        if (
            result.get("status") != "REGISTRATION_CONFIRMED"
            or result.get("request_key") != current["pk"]
            or result.get("request_fingerprint")
            != current["request_fingerprint"]
            or result.get("approval_revision")
            != int(current["approval_revision"])
            or result.get("approval_reference")
            != current["approval_reference"]
            or result.get("candidate_reference")
            != current["assessment"]["candidate_reference"]
        ):
            raise ContractRegistrationBlocked(
                "Registration completion does not match "
                "the current approval"
            )

        record = {
            "pk": current["pk"],
            "sk": "REGISTRATION",
            "record_type": "CONTRACT_REGISTRATION",
            "result": copy.deepcopy(result),
        }

        existing = self.registry._read(
            record["pk"], record["sk"]
        )

        if existing is not None and existing != record:
            raise ContractRegistrationBlocked(
                "A different registration completion "
                "is already recorded"
            )

        actions = [
            human_approval_check(
                registry=self.registry,
                request_key=current["pk"],
                request_fingerprint=current["request_fingerprint"],
                revision=current["approval_revision"],
                evidence_reference=current["approval_reference"],
            ),
            {
                "ConditionCheck": {
                    "TableName": self.registry.table_name,
                    "Key": self.registry._encode(
                        {
                            "pk": current["pk"],
                            "sk": "REGISTRATION_AUTHORIZATION",
                        }
                    ),
                    "ConditionExpression": (
                        "request_fingerprint = :fingerprint "
                        "AND approval_revision = :revision "
                        "AND approval_reference = :evidence"
                    ),
                    "ExpressionAttributeValues": self.registry._encode(
                        {
                            ":fingerprint": current[
                                "request_fingerprint"
                            ],
                            ":revision": int(
                                current["approval_revision"]
                            ),
                            ":evidence": current["approval_reference"],
                        }
                    ),
                }
            },
        ]

        if existing is None:
            actions.append(self.registry._put_new(record))
        else:
            actions.append(
                {
                    "ConditionCheck": {
                        "TableName": self.registry.table_name,
                        "Key": self.registry._encode(
                            {
                                "pk": record["pk"],
                                "sk": record["sk"],
                            }
                        ),
                        "ConditionExpression": "#result = :result",
                        "ExpressionAttributeNames": {
                            "#result": "result"
                        },
                        "ExpressionAttributeValues": (
                            self.registry._encode(
                                {":result": result}
                            )
                        ),
                    }
                }
            )

        try:
            self.registry.client.transact_write_items(
                TransactItems=actions
            )
        except ClientError as error:
            if (
                error.response["Error"]["Code"]
                != "TransactionCanceledException"
            ):
                raise

            reasons = error.response.get(
                "CancellationReasons", []
            )

            if any(
                reason.get("Code") == "ConditionalCheckFailed"
                for reason in reasons[:2]
            ):
                raise ContractRegistrationBlocked(
                    "Approval changed before registration "
                    "completion was recorded"
                ) from error

            if (
                len(reasons) > 2
                and reasons[2].get("Code")
                == "ConditionalCheckFailed"
            ):
                raise RuntimeError(
                    "Registration completion changed concurrently; "
                    "retry the same request to reconcile it"
                ) from error

            raise

        return copy.deepcopy(record)