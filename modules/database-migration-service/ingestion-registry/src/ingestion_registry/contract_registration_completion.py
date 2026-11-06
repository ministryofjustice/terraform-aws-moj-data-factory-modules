import copy

from botocore.exceptions import ClientError
from ingestion_registry.contract_authorisation import human_approval_check

from ingestion_registry.contract_registration import (
    ContractRegistrationBlocked,
)
from ingestion_registry.contract_authorisation import (
    policy_acceptance_check,
)


class ContractRegistrationCompletion:
    def __init__(
        self,
        *,
        registrar,
        approval_gate,
        policy_authorisation=None,
    ):
        self.registrar = registrar
        self.approval_gate = approval_gate
        self.registry = approval_gate.lifecycle.registry
        self.policy_authorisation = policy_authorisation

        if (
            policy_authorisation is not None
            and policy_authorisation.registry is not self.registry
        ):
            raise ValueError(
                "Registration completion and policy authorisation "
                "must use the same Registry"
            )

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
        if "authorisation" in result:
            return self._complete_policy(
                table=table,
                request_id=request_id,
                contract_version=contract_version,
                result=result,
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

    def _complete_policy(
        self,
        *,
        table,
        request_id,
        contract_version,
        result,
    ):
        if self.policy_authorisation is None:
            raise ContractRegistrationBlocked(
                "Policy registration completion is not configured"
            )

        current = self.policy_authorisation.authorise_registration(
            table=table,
            request_id=request_id,
            contract_version=contract_version,
        )

        verified = self.policy_authorisation.verify(
            table=table,
            request_id=request_id,
            contract_version=contract_version,
        )

        expected_receipt = {
            "pk": verified["request_key"],
            "sk": "REGISTRATION_AUTHORIZATION",
            "record_type": "CONTRACT_REGISTRATION_AUTHORIZATION",
            "authorisation_kind": "POLICY",
            "request_id": verified["request_id"],
            "request_fingerprint": verified["request_fingerprint"],
            "table_identity": verified["table_identity"],
            "contract_version": contract_version,
            "authorisation_revision": verified["expected_revision"],
            "authorisation_evidence_reference": verified[
                "evidence_reference"
            ],
            "assessment": verified["assessment"],
            "policy": verified["policy"],
            "decision": verified["decision"],
        }

        expected_authorisation = {
            "kind": "POLICY",
            "request_key": verified["request_key"],
            "request_fingerprint": verified["request_fingerprint"],
            "revision": verified["expected_revision"],
            "evidence_reference": verified["evidence_reference"],
            "candidate_reference": verified["assessment"][
                "candidate_reference"
            ],
            "policy": verified["policy"],
            "contract_version": contract_version,
        }

        if (
            current != expected_receipt
            or result.get("status") != "REGISTRATION_CONFIRMED"
            or result.get("request_key") != verified["request_key"]
            or result.get("request_fingerprint")
            != verified["request_fingerprint"]
            or result.get("candidate_reference")
            != verified["assessment"]["candidate_reference"]
            or result.get("authorisation") != expected_authorisation
            or not isinstance(result.get("registered_contract"), dict)
            or result["registered_contract"].get("contract_version")
            != contract_version
            or "approval_reference" in result
            or "approval_revision" in result
        ):
            raise ContractRegistrationBlocked(
                "Registration completion does not match "
                "the current policy authorisation"
            )

        record = {
            "pk": verified["request_key"],
            "sk": "REGISTRATION",
            "record_type": "CONTRACT_REGISTRATION",
            "result": copy.deepcopy(result),
        }

        existing = self.registry._read(
            record["pk"],
            record["sk"],
        )

        if existing is not None and existing != record:
            raise ContractRegistrationBlocked(
                "A different registration completion "
                "is already recorded"
            )

        actions = [
            policy_acceptance_check(
                registry=self.registry,
                verified=verified,
            ),
            self._policy_receipt_check(expected_receipt),
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
                        "ConditionExpression": (
                            "#kind = :kind AND #result = :result"
                        ),
                        "ExpressionAttributeNames": {
                            "#kind": "record_type",
                            "#result": "result",
                        },
                        "ExpressionAttributeValues": (
                            self.registry._encode(
                                {
                                    ":kind": "CONTRACT_REGISTRATION",
                                    ":result": result,
                                }
                            )
                        ),
                    }
                }
            )

        try:
            self.registry.client.transact_write_items(
                TransactItems=actions,
            )
        except ClientError as error:
            if (
                error.response["Error"]["Code"]
                != "TransactionCanceledException"
            ):
                raise

            reasons = error.response.get(
                "CancellationReasons",
                [],
            )

            if any(
                reason.get("Code") == "ConditionalCheckFailed"
                for reason in reasons[:2]
            ):
                raise ContractRegistrationBlocked(
                    "Policy decision or authorisation changed "
                    "before registration completion was recorded"
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

    def _policy_receipt_check(self, record):
        fields = sorted(set(record) - {"pk", "sk"})

        names = {
            f"#field{index}": field
            for index, field in enumerate(fields)
        }
        values = {
            f":value{index}": record[field]
            for index, field in enumerate(fields)
        }

        return {
            "ConditionCheck": {
                "TableName": self.registry.table_name,
                "Key": self.registry._encode(
                    {
                        "pk": record["pk"],
                        "sk": record["sk"],
                    }
                ),
                "ConditionExpression": " AND ".join(
                    f"#field{index} = :value{index}"
                    for index in range(len(fields))
                ),
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": self.registry._encode(
                    values
                ),
            }
        }