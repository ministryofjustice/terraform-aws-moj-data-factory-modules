import copy
from dataclasses import asdict

from botocore.exceptions import ClientError

from ingestion_registry.references import S3ObjectReference


class ContractReuseBlocked(ValueError):
    """The assessed baseline cannot safely be selected for reuse."""


class ContractReuse:
    def __init__(
        self,
        *,
        lifecycle,
        current_lookup,
        contract_verifier,
    ):
        if lifecycle.registry is not current_lookup.registry:
            raise ValueError(
                "Contract reuse and current lookup must use the same Registry"
            )

        self.lifecycle = lifecycle
        self.registry = lifecycle.registry
        self.current_lookup = current_lookup
        self.verifier = contract_verifier

    def complete(
        self,
        *,
        table,
        request_id: str,
        expected_request_fingerprint: str,
    ) -> dict:
        request_key = self.lifecycle._key(table, request_id)

        if (
            not isinstance(expected_request_fingerprint, str)
            or len(expected_request_fingerprint) != 64
            or any(
                character not in "0123456789abcdef"
                for character in expected_request_fingerprint
            )
        ):
            raise ValueError("Invalid expected request fingerprint")

        request = self.lifecycle.read(
            table=table,
            request_id=request_id,
        )

        if (
            request is None
            or request.get("table_identity") != asdict(table)
            or request.get("request_id") != request_id
            or request.get("request_fingerprint")
            != expected_request_fingerprint
        ):
            raise ContractReuseBlocked(
                "Reuse does not match the exact assessment request"
            )

        assessment = request.get("assessment", {})
        decision = request.get("decision", {})

        if (
            assessment.get("validation") != "VALID"
            or assessment.get("compatibility") != "NO_CHANGE"
            or decision.get("action") != "REUSE_EXISTING"
        ):
            raise ContractReuseBlocked(
                "Only a validated no-change assessment can reuse its baseline"
            )

        baseline_data = assessment.get("baseline_reference")

        try:
            baseline = S3ObjectReference(**baseline_data)
        except (TypeError, ValueError) as error:
            raise ContractReuseBlocked(
                "Reuse requires an exact baseline reference"
            ) from error

        previous = self.registry._read(request_key, "REUSE")

        if previous is not None:
            return self._replay(
                table,
                request,
                previous,
                baseline,
            )

        if request.get("status") != "REUSE_PENDING":
            raise ContractReuseBlocked(
                "Contract request is not pending baseline reuse"
            )

        current = self.current_lookup.resolve(table=table)

        if current is None:
            raise ContractReuseBlocked(
                "No approved current contract is available"
            )

        selection = current["selection"]
        registered = selection["registered_contract"]

        if registered["object_reference"] != asdict(baseline):
            raise ContractReuseBlocked(
                "The approved baseline changed; request a new assessment"
            )

        result = {
            "status": "REUSED",
            "request_id": request_id,
            "request_fingerprint": expected_request_fingerprint,
            "registered_contract": copy.deepcopy(registered),
            "selection": copy.deepcopy(selection),
        }

        updated = copy.deepcopy(request)
        updated["status"] = "REUSED"
        updated["revision"] = int(request["revision"]) + 1

        record = {
            "pk": request_key,
            "sk": "REUSE",
            "record_type": "CONTRACT_REUSE",
            "result": result,
        }

        try:
            self.registry.client.transact_write_items(
                TransactItems=[
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
                                "AND registered_contract = :contract "
                                "AND approval_reference = :approval "
                                "AND request_key = :request"
                            ),
                            "ExpressionAttributeNames": {
                                "#revision": "revision",
                            },
                            "ExpressionAttributeValues": (
                                self.registry._encode(
                                    {
                                        ":revision": selection["revision"],
                                        ":contract": registered,
                                        ":approval": selection[
                                            "approval_reference"
                                        ],
                                        ":request": selection["request_key"],
                                    }
                                )
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
                            "ExpressionAttributeValues": (
                                self.registry._encode(
                                    {
                                        ":approved": "APPROVED",
                                        ":revision": current[
                                            "approval_revision"
                                        ],
                                        ":fingerprint": selection[
                                            "request_fingerprint"
                                        ],
                                        ":approval": selection[
                                            "approval_reference"
                                        ],
                                    }
                                )
                            ),
                        }
                    },
                    self._request_write(request, updated),
                    self.registry._put_new(record),
                ]
            )
        except ClientError as error:
            if (
                error.response["Error"]["Code"]
                != "TransactionCanceledException"
            ):
                raise

            reasons = {
                reason.get("Code")
                for reason in error.response.get(
                    "CancellationReasons",
                    [],
                )
            }

            if not reasons or not reasons <= {
                None,
                "None",
                "ConditionalCheckFailed",
                "TransactionConflict",
            }:
                raise
            previous = self.registry._read(request_key, "REUSE")
            latest = self.lifecycle.read(
                table=table,
                request_id=request_id,
            )

            if previous is not None and latest is not None:
                return self._replay(
                    table,
                    latest,
                    previous,
                    baseline,
                    expected_fingerprint=expected_request_fingerprint,
                )

            raise ContractReuseBlocked(
                "Baseline or assessment changed during reuse; "
                "read and reconcile the request"
            ) from error

        return copy.deepcopy(result)

    def _replay(
        self,
        table,
        request,
        record,
        baseline,
        expected_fingerprint=None,
    ):
        expected_fingerprint = (
            request["request_fingerprint"]
            if expected_fingerprint is None
            else expected_fingerprint
        )

        result = record.get("result")

        if (
            record.get("record_type") != "CONTRACT_REUSE"
            or record.get("pk") != request["pk"]
            or record.get("sk") != "REUSE"
            or request.get("status") != "REUSED"
            or request.get("request_fingerprint")
            != expected_fingerprint
            or not isinstance(result, dict)
            or result.get("status") != "REUSED"
            or result.get("request_id") != request["request_id"]
            or result.get("request_fingerprint")
            != expected_fingerprint
        ):
            raise ContractReuseBlocked(
                "Recorded reuse does not match the completed assessment"
            )

        registered = result.get("registered_contract")
        selection = result.get("selection")

        if (
            not isinstance(registered, dict)
            or not isinstance(selection, dict)
            or selection.get("registered_contract") != registered
            or registered.get("object_reference") != asdict(baseline)
        ):
            raise ContractReuseBlocked(
                "Recorded reuse does not match the assessed baseline"
            )

        verified = self.verifier.verify(
            identity=table,
            contract_version=registered["contract_version"],
        )

        if asdict(verified) != registered:
            raise ContractReuseBlocked(
                "Recorded baseline no longer matches registered content"
            )

        self.registry.client.transact_write_items(
            TransactItems=[
                {
                    "ConditionCheck": {
                        "TableName": self.registry.table_name,
                        "Key": self.registry._encode(
                            {"pk": request["pk"], "sk": "META"}
                        ),
                        "ConditionExpression": (
                            "#status = :status "
                            "AND #revision = :revision "
                            "AND request_fingerprint = :fingerprint"
                        ),
                        "ExpressionAttributeNames": {
                            "#status": "status",
                            "#revision": "revision",
                        },
                        "ExpressionAttributeValues": self.registry._encode(
                            {
                                ":status": "REUSED",
                                ":revision": request["revision"],
                                ":fingerprint": expected_fingerprint,
                            }
                        ),
                    }
                },
                {
                    "ConditionCheck": {
                        "TableName": self.registry.table_name,
                        "Key": self.registry._encode(
                            {"pk": request["pk"], "sk": "REUSE"}
                        ),
                        "ConditionExpression": (
                            "#kind = :kind AND #result = :result"
                        ),
                        "ExpressionAttributeNames": {
                            "#kind": "record_type",
                            "#result": "result",
                        },
                        "ExpressionAttributeValues": self.registry._encode(
                            {
                                ":kind": "CONTRACT_REUSE",
                                ":result": result,
                            }
                        ),
                    }
                },
            ]
        )

        return copy.deepcopy(result)

    def _request_write(self, original, updated):
        return {
            "Put": {
                "TableName": self.registry.table_name,
                "Item": self.registry._encode(updated),
                "ConditionExpression": (
                    "#status = :pending "
                    "AND #revision = :revision "
                    "AND request_fingerprint = :fingerprint "
                    "AND assessment = :assessment"
                ),
                "ExpressionAttributeNames": {
                    "#status": "status",
                    "#revision": "revision",
                },
                "ExpressionAttributeValues": self.registry._encode(
                    {
                        ":pending": "REUSE_PENDING",
                        ":revision": original["revision"],
                        ":fingerprint": original["request_fingerprint"],
                        ":assessment": original["assessment"],
                    }
                ),
            }
        }