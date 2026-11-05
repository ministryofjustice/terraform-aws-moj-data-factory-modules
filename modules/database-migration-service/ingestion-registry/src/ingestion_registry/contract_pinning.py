from ingestion_registry.contracts import RegisteredContract
from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference


class ContractSelectionUnavailable(ValueError):
    """No current approved contract is available for this ingestion."""


class CurrentContractPinning:
    def __init__(self, *, lookup, contracts):
        self.lookup = lookup
        self.contracts = contracts
        self.progress = contracts.progress
        self.registry = self.progress.registry

        if self.lookup.registry is not self.registry:
            raise ValueError(
                "Lookup and pinning must use the same Registry instance"
            )

    def pin(
        self,
        claim,
        *,
        update_id: str,
        expected_revision: int,
    ) -> dict:
        ingestion = self.registry._read(
            f"INGESTION#{claim.ingestion_id}",
            "META",
        )

        if ingestion is None:
            raise ValueError("Ingestion does not exist")

        table = TableIdentity(**ingestion["table_identity"])
        existing = self.progress.read_stage(
            ingestion_id=claim.ingestion_id,
            stage="CONTRACT",
        )

        if existing is not None and existing["status"] == "SUCCEEDED":
            reference = existing["result_reference"]

            if reference.get("kind") != "registered-contract":
                raise ValueError(
                    "Completed CONTRACT stage has an unsupported result"
                )

            contract = RegisteredContract(
                contract_id=reference["contract_id"],
                contract_version=reference["contract_version"],
                contract_fingerprint=reference["contract_fingerprint"],
                object_reference=S3ObjectReference(
                    bucket_name=reference["contract_bucket_name"],
                    object_key=reference["contract_object_key"],
                    version_id=reference["contract_object_version_id"],
                    sha256=reference["contract_object_sha256"],
                ),
            )
            approval = S3ObjectReference(
                bucket_name=reference["approval_bucket_name"],
                object_key=reference["approval_object_key"],
                version_id=reference["approval_object_version_id"],
                sha256=reference["approval_object_sha256"],
            )

            return self.contracts.pin(
                claim,
                contract=contract,
                approval_reference=approval,
                update_id=existing["update_id"],
                expected_revision=int(existing["revision"]) - 1,
            )

        resolved = self.lookup.resolve(table=table)

        if resolved is None:
            raise ContractSelectionUnavailable(
                "No current approved contract is available"
            )

        selection = resolved["selection"]
        registered = selection["registered_contract"]
        contract = RegisteredContract(
            contract_id=registered["contract_id"],
            contract_version=registered["contract_version"],
            contract_fingerprint=registered["contract_fingerprint"],
            object_reference=S3ObjectReference(
                **registered["object_reference"]
            ),
        )
        approval = S3ObjectReference(
            **selection["approval_reference"]
        )

        checks = [
            {
                "ConditionCheck": {
                    "TableName": self.registry.table_name,
                    "Key": self.registry._encode(
                        {
                            "pk": table.partition_key,
                            "sk": "CONTRACT_CURRENT",
                        }
                    ),
                    "ConditionExpression": (
                        "#revision = :revision "
                        "AND request_key = :request "
                        "AND registered_contract = :contract "
                        "AND approval_reference = :approval"
                    ),
                    "ExpressionAttributeNames": {
                        "#revision": "revision",
                    },
                    "ExpressionAttributeValues": self.registry._encode(
                        {
                            ":revision": int(selection["revision"]),
                            ":request": selection["request_key"],
                            ":contract": registered,
                            ":approval": selection["approval_reference"],
                        }
                    ),
                }
            },
            {
                "ConditionCheck": {
                    "TableName": self.registry.table_name,
                    "Key": self.registry._encode(
                        {
                            "pk": selection["request_key"],
                            "sk": "META",
                        }
                    ),
                    "ConditionExpression": (
                        "#status = :approved "
                        "AND #revision = :revision "
                        "AND request_fingerprint = :fingerprint "
                        "AND #review.#evidence = :approval"
                    ),
                    "ExpressionAttributeNames": {
                        "#status": "status",
                        "#revision": "revision",
                        "#review": "review",
                        "#evidence": "evidence_reference",
                    },
                    "ExpressionAttributeValues": self.registry._encode(
                        {
                            ":approved": "APPROVED",
                            ":revision": resolved["approval_revision"],
                            ":fingerprint": selection[
                                "request_fingerprint"
                            ],
                            ":approval": selection["approval_reference"],
                        }
                    ),
                }
            },
        ]

        return self.contracts.pin(
            claim,
            contract=contract,
            approval_reference=approval,
            update_id=update_id,
            expected_revision=expected_revision,
            condition_checks=checks,
        )