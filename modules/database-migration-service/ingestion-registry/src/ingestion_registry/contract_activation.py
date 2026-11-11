import copy
from dataclasses import asdict
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from ingestion_registry.identity import TableIdentity
from ingestion_registry.contract_authorisation import (
    human_approval_check,
    policy_acceptance_check,
)


class ContractActivationConflict(ValueError):
    """Activation conflicts with approval or the current contract."""


class ContractActivation:
    def __init__(self, *, completion, lifecycle):
        self.completion = completion
        self.lifecycle = lifecycle
        self.registry = lifecycle.registry

    def read_current(self, *, table: TableIdentity) -> dict | None:
        if not isinstance(table, TableIdentity):
            raise ValueError("table must be a TableIdentity")

        return self.registry._read(
            table.partition_key,
            "CONTRACT_CURRENT",
        )

    def activate(
        self,
        *,
        table: TableIdentity,
        request_id: str,
        contract_version: str,
    ) -> dict:
        if not isinstance(table, TableIdentity):
            raise ValueError("table must be a TableIdentity")

        registration = self.completion.complete(
            table=table,
            request_id=request_id,
            contract_version=contract_version,
        )
        result = registration["result"]

        request = self.lifecycle.read(
            table=table,
            request_id=request_id,
        )

        if "authorisation" in result:
            guards = self._policy_guards(
                table=table,
                request_id=request_id,
                contract_version=contract_version,
                request=request,
                result=result,
            )
        else:
            if (
                request is None
                or request["pk"] != result["request_key"]
                or request["status"] != "APPROVED"
                or request["request_fingerprint"]
                != result["request_fingerprint"]
                or int(request["revision"])
                != result["approval_revision"]
                or request["review"]["evidence_reference"]
                != result["approval_reference"]
                or request["assessment"]["candidate_reference"]
                != result["candidate_reference"]
            ):
                raise ContractActivationConflict(
                    "Registration does not match the current approved request"
                )

            guards = [
                human_approval_check(
                    registry=self.registry,
                    request_key=request["pk"],
                    request_fingerprint=request["request_fingerprint"],
                    revision=request["revision"],
                    evidence_reference=result["approval_reference"],
                )
            ]

        registered = result["registered_contract"]

        if registered["contract_version"] != contract_version:
            raise ContractActivationConflict(
                "Registration has a different contract version"
            )

        request_pk = request["pk"]
        previous_activation = self.registry._read(
            request_pk,
            "ACTIVATION",
        )

        guards.append(
            {
                "ConditionCheck": {
                    "TableName": self.registry.table_name,
                    "Key": self.registry._encode(
                        {"pk": request_pk, "sk": "REGISTRATION"}
                    ),
                    "ConditionExpression": "#result = :result",
                    "ExpressionAttributeNames": {
                        "#result": "result",
                    },
                    "ExpressionAttributeValues": self.registry._encode(
                        {":result": result}
                    ),
                }
            }
        )

        if previous_activation is not None:
            if (
                previous_activation.get("registration_result") != result
                or previous_activation.get("table_identity")
                != asdict(table)
            ):
                raise ContractActivationConflict(
                    "A different activation is already recorded"
                )

            guards.append(
                {
                    "ConditionCheck": {
                        "TableName": self.registry.table_name,
                        "Key": self.registry._encode(
                            {"pk": request_pk, "sk": "ACTIVATION"}
                        ),
                        "ConditionExpression": (
                            "registration_result = :result"
                        ),
                        "ExpressionAttributeValues": (
                            self.registry._encode({":result": result})
                        ),
                    }
                }
            )
            self._commit(guards)
            return copy.deepcopy(previous_activation)

        baseline = request["assessment"]["baseline_reference"]
        current = self.read_current(table=table)

        if baseline is None:
            if current is not None:
                raise ContractActivationConflict(
                    "First-contract approval is stale: "
                    "a current contract already exists"
                )
        elif (
            current is None
            or current.get("table_identity") != asdict(table)
            or current["registered_contract"]["object_reference"]
            != baseline
        ):
            raise ContractActivationConflict(
                "The approved comparison baseline is no longer current"
            )

        now = datetime.now(timezone.utc).isoformat()
        next_revision = (
            1 if current is None else int(current["revision"]) + 1
        )

        selection = {
            "pk": table.partition_key,
            "sk": "CONTRACT_CURRENT",
            "record_type": "CONTRACT_CURRENT",
            "table_identity": asdict(table),
            "revision": next_revision,
            "request_key": request_pk,
            "request_fingerprint": request["request_fingerprint"],
            "registered_contract": copy.deepcopy(registered),
            "activated_at": now,
        }

        if "authorisation" in result:
            selection["authorisation"] = copy.deepcopy(
                result["authorisation"]
            )
        else:
            selection["approval_reference"] = copy.deepcopy(
                result["approval_reference"]
            )
        activation = {
            "pk": request_pk,
            "sk": "ACTIVATION",
            "record_type": "CONTRACT_ACTIVATION",
            "table_identity": asdict(table),
            "registration_result": copy.deepcopy(result),
            "selection": copy.deepcopy(selection),
        }

        selection_put = {
            "TableName": self.registry.table_name,
            "Item": self.registry._encode(selection),
            "ConditionExpression": "attribute_not_exists(pk)",
        }

        if current is not None:
            selection_put.update(
                {
                    "ConditionExpression": (
                        "#revision = :previous "
                        "AND registered_contract = :contract"
                    ),
                    "ExpressionAttributeNames": {
                        "#revision": "revision"
                    },
                    "ExpressionAttributeValues": (
                        self.registry._encode(
                            {
                                ":previous": int(current["revision"]),
                                ":contract": current[
                                    "registered_contract"
                                ],
                            }
                        )
                    ),
                }
            )

        self._commit(
            guards
            + [
                {"Put": selection_put},
                self.registry._put_new(activation),
            ]
        )

        return copy.deepcopy(activation)

    def _commit(self, actions):
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

            if any(
                reason.get("Code") == "ConditionalCheckFailed"
                for reason in error.response.get(
                    "CancellationReasons", []
                )
            ):
                raise ContractActivationConflict(
                    "Activation state changed concurrently; "
                    "retry the same request to reconcile it"
                ) from error

            raise

    def _policy_guards(
        self,
        *,
        table,
        request_id,
        contract_version,
        request,
        result,
    ):
        policy = self.completion.policy_authorisation

        if policy is None:
            raise ContractActivationConflict(
                "Policy activation is not configured"
            )

        receipt = policy.authorise_registration(
            table=table,
            request_id=request_id,
            contract_version=contract_version,
        )

        verified = policy.verify(
            table=table,
            request_id=request_id,
            contract_version=contract_version,
        )

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

        if (
            request is None
            or request.get("status") != "POLICY_ACCEPTED"
            or request.get("table_identity") != asdict(table)
            or request.get("request_id") != request_id
            or request.get("pk") != verified["request_key"]
            or request.get("request_fingerprint")
            != verified["request_fingerprint"]
            or request.get("revision") != verified["expected_revision"]
            or request.get("assessment") != verified["assessment"]
            or request.get("policy") != verified["policy"]
            or request.get("decision") != verified["decision"]
            or receipt != expected_receipt
            or result.get("status") != "REGISTRATION_CONFIRMED"
            or result.get("request_key") != verified["request_key"]
            or result.get("request_fingerprint")
            != verified["request_fingerprint"]
            or result.get("candidate_reference")
            != verified["assessment"]["candidate_reference"]
            or result.get("authorisation") != expected_authorisation
            or "approval_reference" in result
            or "approval_revision" in result
        ):
            raise ContractActivationConflict(
                "Registration does not match the current policy authorisation"
            )

        return [
            policy_acceptance_check(
                registry=self.registry,
                verified=verified,
            ),
            self.completion._policy_receipt_check(
                expected_receipt
            ),
        ]