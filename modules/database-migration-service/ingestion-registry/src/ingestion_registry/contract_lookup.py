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
    ):
        self.registry = registry
        self.approval_gate = approval_gate
        self.verifier = verifier

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