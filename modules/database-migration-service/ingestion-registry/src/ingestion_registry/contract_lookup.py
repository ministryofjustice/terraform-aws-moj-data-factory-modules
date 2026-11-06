import copy
from dataclasses import asdict

from ingestion_registry.identity import TableIdentity


class ContractLookupBlocked(ValueError):
    """The current selection has missing or inconsistent evidence."""


class CurrentContractLookup:
    def __init__(
        self,
        *,
        registry,
        approval_gate,
        verifier,
        policy_authorisation=None,
    ):
        if (
            policy_authorisation is not None
            and policy_authorisation.registry is not registry
        ):
            raise ValueError(
                "Lookup and policy authorisation must use the same Registry"
            )

        self.registry = registry
        self.approval_gate = approval_gate
        self.verifier = verifier
        self.policy_authorisation = policy_authorisation
    def resolve(self, *, table: TableIdentity) -> dict | None:
        if not isinstance(table, TableIdentity):
            raise ValueError("table must be a TableIdentity")

        selection = self.registry._read(
            table.partition_key,
            "CONTRACT_CURRENT",
        )

        if selection is None:
            return None

        if selection.get("table_identity") != asdict(table):
            raise ContractLookupBlocked(
                "Current contract selection has a different table identity"
            )

        if "authorisation" in selection:
            return self._resolve_policy(
                table=table,
                selection=selection,
            )

        request_key = selection.get("request_key")

        if (
            not isinstance(request_key, str)
            or not request_key.startswith("CONTRACTREQUEST#")
        ):
            raise ContractLookupBlocked(
                "Current contract has an invalid lifecycle request reference"
            )

        request = self.registry._read(request_key, "META")

        if (
            request is None
            or request.get("table_identity") != asdict(table)
            or request.get("request_fingerprint")
            != selection.get("request_fingerprint")
        ):
            raise ContractLookupBlocked(
                "Current contract does not match its lifecycle request"
            )

        approval = self.approval_gate.verify(
            table=table,
            request_id=request["request_id"],
        )

        if (
            approval["request_key"] != request_key
            or approval["request_fingerprint"]
            != selection["request_fingerprint"]
            or approval["approval_reference"]
            != selection.get("approval_reference")
        ):
            raise ContractLookupBlocked(
                "Current contract does not match its verified approval"
            )

        activation = self.registry._read(
            request_key, "ACTIVATION"
        )
        registration = self.registry._read(
            request_key, "REGISTRATION"
        )

        if (
            activation is None
            or activation.get("selection") != selection
            or registration is None
            or activation.get("registration_result")
            != registration.get("result")
        ):
            raise ContractLookupBlocked(
                "Current contract lacks matching activation "
                "and registration completion"
            )

        registered = selection.get("registered_contract")

        if (
            not isinstance(registered, dict)
            or registration["result"].get("status")
            != "REGISTRATION_CONFIRMED"
            or registration["result"].get("registered_contract")
            != registered
            or registration["result"].get("approval_reference")
            != approval["approval_reference"]
            or registration["result"].get("approval_revision")
            != approval["expected_revision"]
            or registration["result"].get("candidate_reference")
            != approval["assessment"]["candidate_reference"]
        ):
            raise ContractLookupBlocked(
                "Current contract differs from its approved registration"
            )

        contract = self.verifier.verify(
            identity=table,
            contract_version=registered["contract_version"],
        )

        if asdict(contract) != registered:
            raise ContractLookupBlocked(
                "Verified registration differs from the current selection"
            )

        latest_selection = self.registry._read(
            table.partition_key,
            "CONTRACT_CURRENT",
        )
        latest_request = self.registry._read(request_key, "META")

        if (
            latest_selection != selection
            or latest_request != request
        ):
            raise ContractLookupBlocked(
                "Contract selection or decision changed during lookup; "
                "retry to resolve the current contract"
            )

        return {
            "selection": copy.deepcopy(selection),
            "approval_revision": approval["expected_revision"],
        }

    def _resolve_policy(self, *, table, selection):
        if self.policy_authorisation is None:
            raise ContractLookupBlocked(
                "Policy contract lookup is not configured"
            )

        authorisation = selection.get("authorisation")
        registered = selection.get("registered_contract")
        request_key = selection.get("request_key")

        if (
            not isinstance(authorisation, dict)
            or authorisation.get("kind") != "POLICY"
            or not isinstance(registered, dict)
            or not isinstance(request_key, str)
            or not request_key.startswith("CONTRACTREQUEST#")
            or "approval_reference" in selection
        ):
            raise ContractLookupBlocked(
                "Policy selection has invalid authorisation evidence"
            )

        request = self.registry._read(request_key, "META")

        if (
            request is None
            or request.get("table_identity") != asdict(table)
            or request.get("status") != "POLICY_ACCEPTED"
            or request.get("request_fingerprint")
            != selection.get("request_fingerprint")
        ):
            raise ContractLookupBlocked(
                "Policy selection does not match its lifecycle request"
            )

        verified = self.policy_authorisation.verify(
            table=table,
            request_id=request["request_id"],
            contract_version=registered["contract_version"],
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
            "contract_version": registered["contract_version"],
        }

        if (
            authorisation != expected_authorisation
            or verified["request_key"] != request_key
            or verified["request_fingerprint"]
            != selection["request_fingerprint"]
        ):
            raise ContractLookupBlocked(
                "Current selection differs from verified policy authorisation"
            )

        receipt = self.registry._read(
            request_key,
            "REGISTRATION_AUTHORIZATION",
        )

        expected_receipt = {
            "pk": request_key,
            "sk": "REGISTRATION_AUTHORIZATION",
            "record_type": "CONTRACT_REGISTRATION_AUTHORIZATION",
            "authorisation_kind": "POLICY",
            "request_id": verified["request_id"],
            "request_fingerprint": verified["request_fingerprint"],
            "table_identity": verified["table_identity"],
            "contract_version": registered["contract_version"],
            "authorisation_revision": verified["expected_revision"],
            "authorisation_evidence_reference": verified[
                "evidence_reference"
            ],
            "assessment": verified["assessment"],
            "policy": verified["policy"],
            "decision": verified["decision"],
        }

        if receipt != expected_receipt:
            raise ContractLookupBlocked(
                "Policy selection lacks matching registration authorisation"
            )

        registration = self.registry._read(
            request_key,
            "REGISTRATION",
        )
        activation = self.registry._read(
            request_key,
            "ACTIVATION",
        )

        expected_result = {
            "status": "REGISTRATION_CONFIRMED",
            "request_key": request_key,
            "request_fingerprint": verified["request_fingerprint"],
            "candidate_reference": verified["assessment"][
                "candidate_reference"
            ],
            "registered_contract": registered,
            "authorisation": expected_authorisation,
        }

        if (
            registration is None
            or registration.get("record_type")
            != "CONTRACT_REGISTRATION"
            or registration.get("result") != expected_result
            or activation is None
            or activation.get("record_type") != "CONTRACT_ACTIVATION"
            or activation.get("table_identity") != asdict(table)
            or activation.get("selection") != selection
            or activation.get("registration_result") != expected_result
        ):
            raise ContractLookupBlocked(
                "Policy selection lacks matching registration and activation"
            )

        contract = self.verifier.verify(
            identity=table,
            contract_version=registered["contract_version"],
        )

        if asdict(contract) != registered:
            raise ContractLookupBlocked(
                "Verified registration differs from the policy selection"
            )

        snapshots = [
            (table.partition_key, "CONTRACT_CURRENT", selection),
            (request_key, "META", request),
            (request_key, "REGISTRATION_AUTHORIZATION", receipt),
            (request_key, "REGISTRATION", registration),
            (request_key, "ACTIVATION", activation),
        ]

        if any(
            self.registry._read(pk, sk) != original
            for pk, sk, original in snapshots
        ):
            raise ContractLookupBlocked(
                "Policy selection or evidence changed during lookup; "
                "retry to resolve the current contract"
            )

        return {
            "selection": copy.deepcopy(selection),
            "authorisation_kind": "POLICY",
            "authorisation_revision": verified["expected_revision"],
        }