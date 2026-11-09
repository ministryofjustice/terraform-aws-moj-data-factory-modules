import copy
from dataclasses import asdict

from botocore.exceptions import ClientError


class ContractAssessmentRequestBlocked(ValueError):
    """An assessment request conflicts with its pinned generation context."""


class ContractAssessmentRequests:
    def __init__(
        self,
        *,
        generation_completion,
        current_lookup,
        assessor_revision: str,
    ):
        if generation_completion.registry is not current_lookup.registry:
            raise ValueError(
                "Assessment requests and lookup must use the same Registry"
            )

        if (
            not isinstance(assessor_revision, str)
            or not assessor_revision
            or assessor_revision != assessor_revision.strip()
            or len(assessor_revision.encode("utf-8")) > 256
        ):
            raise ValueError(
                "assessor_revision must contain 1 to 256 UTF-8 bytes "
                "without surrounding whitespace"
            )

        self.completion = generation_completion
        self.lookup = current_lookup
        self.registry = generation_completion.registry
        self.assessor_revision = assessor_revision

    def prepare(
        self,
        *,
        table,
        reservation_key: str,
        preparation_fingerprint: str,
    ) -> dict:
        reservation = self.completion._reservation(
            table,
            reservation_key,
            preparation_fingerprint,
        )

        generation = self.registry._read(
            reservation_key,
            "GENERATION",
        )

        if generation is None:
            raise ContractAssessmentRequestBlocked(
                "Generation completion must exist before requesting assessment"
            )

        generated = self.completion._verify_record(
            generation,
            table,
            reservation,
        )

        existing = self.registry._read(
            reservation_key,
            "ASSESSMENT_REQUEST",
        )

        if existing is not None:
            self._verify_existing(
                table=table,
                reservation=reservation,
                generation=generation,
                generated=generated,
                record=existing,
            )
            record = existing
            selection_check = None
        else:
            current = self.lookup.resolve(table=table)
            selection = (
                None if current is None else current["selection"]
            )

            record = {
                "pk": reservation_key,
                "sk": "ASSESSMENT_REQUEST",
                "record_type": "CONTRACT_ASSESSMENT_REQUEST",
                "preparation_fingerprint": preparation_fingerprint,
                "generation_result": copy.deepcopy(generated),
                "baseline_selection": copy.deepcopy(selection),
                "request": self._request(
                    table=table,
                    generated=generated,
                    selection=selection,
                ),
            }

            selection_check = self._selection_check(
                table=table,
                selection=selection,
            )

        actions = [
            self.completion._reservation_check(reservation),
            self._record_check(
                pk=reservation_key,
                sk="GENERATION",
                field="result",
                value=generated,
            ),
        ]

        if existing is None:
            actions.extend(
                [
                    selection_check,
                    self.registry._put_new(record),
                ]
            )
        else:
            actions.extend(
                [
                    self._record_check(
                        pk=reservation_key,
                        sk="ASSESSMENT_REQUEST",
                        field="request",
                        value=record["request"],
                    ),
                ]
            )
            check = actions[-1]["ConditionCheck"]
            check["ConditionExpression"] += (
                " AND baseline_selection = :baseline "
                "AND generation_result = :generation "
                "AND preparation_fingerprint = :fingerprint "
                "AND record_type = :kind"
            )
            check["ExpressionAttributeValues"].update(
                self.registry._encode(
                    {
                        ":baseline": record["baseline_selection"],
                        ":generation": generated,
                        ":fingerprint": preparation_fingerprint,
                        ":kind": "CONTRACT_ASSESSMENT_REQUEST",
                    }
                )
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

            if any(
                reason.get("Code") == "ConditionalCheckFailed"
                for reason in error.response.get(
                    "CancellationReasons",
                    [],
                )
            ):
                raise ContractAssessmentRequestBlocked(
                    "Generation, baseline or assessment request changed; "
                    "retry the same reservation to reconcile it"
                ) from error

            raise

        return copy.deepcopy(record)

    def _request(self, *, table, generated, selection):
        return {
            "assessment_request_version": 1,
            "request_id": generated["request_id"],
            "identity": {
                "source": table.source,
                "database_name": table.database_name,
                "schema_name": table.schema_name,
                "table_name": table.table_name,
                "contract_version": generated["contract_version"],
            },
            "candidate_reference": copy.deepcopy(
                generated["candidate_reference"]
            ),
            "baseline_reference": (
                None
                if selection is None
                else copy.deepcopy(
                    selection["registered_contract"]["object_reference"]
                )
            ),
            "assessor_revision": self.assessor_revision,
        }

    def _verify_existing(
        self,
        *,
        table,
        reservation,
        generation,
        generated,
        record,
    ):
        selection = record.get("baseline_selection")

        if selection is not None:
            if (
                not isinstance(selection, dict)
                or selection.get("table_identity") != asdict(table)
                or not isinstance(
                    selection.get("registered_contract"),
                    dict,
                )
            ):
                raise ContractAssessmentRequestBlocked(
                    "Recorded assessment baseline is invalid"
                )

            registered = selection["registered_contract"]
            verified = self.lookup.verifier.verify(
                identity=table,
                contract_version=registered["contract_version"],
            )

            if asdict(verified) != registered:
                raise ContractAssessmentRequestBlocked(
                    "Recorded assessment baseline content has changed"
                )

        if (
            record.get("pk") != reservation["pk"]
            or record.get("sk") != "ASSESSMENT_REQUEST"
            or record.get("record_type")
            != "CONTRACT_ASSESSMENT_REQUEST"
            or record.get("preparation_fingerprint")
            != reservation["request_fingerprint"]
            or record.get("generation_result") != generated
            or generation.get("result") != generated
            or record.get("request")
            != self._request(
                table=table,
                generated=generated,
                selection=selection,
            )
        ):
            raise ContractAssessmentRequestBlocked(
                "Existing assessment request has different context"
            )

    def _selection_check(self, *, table, selection):
        check = {
            "TableName": self.registry.table_name,
            "Key": self.registry._encode(
                {
                    "pk": table.partition_key,
                    "sk": "CONTRACT_CURRENT",
                }
            ),
            "ConditionExpression": "attribute_not_exists(pk)",
        }

        if selection is not None:
            evidence_field = (
                "authorisation"
                if "authorisation" in selection
                else "approval_reference"
            )

            check.update(
                {
                    "ConditionExpression": (
                        "#revision = :revision "
                        "AND request_key = :request "
                        "AND request_fingerprint = :fingerprint "
                        "AND registered_contract = :contract "
                        "AND #evidence = :evidence "
                        "AND table_identity = :table"
                    ),
                    "ExpressionAttributeNames": {
                        "#revision": "revision",
                        "#evidence": evidence_field,
                    },
                    "ExpressionAttributeValues": self.registry._encode(
                        {
                            ":revision": selection["revision"],
                            ":request": selection["request_key"],
                            ":fingerprint": selection[
                                "request_fingerprint"
                            ],
                            ":contract": selection["registered_contract"],
                            ":evidence": selection[evidence_field],
                            ":table": asdict(table),
                        }
                    ),
                }
            )

        return {"ConditionCheck": check}

    def _record_check(self, *, pk, sk, field, value):
        return {
            "ConditionCheck": {
                "TableName": self.registry.table_name,
                "Key": self.registry._encode(
                    {"pk": pk, "sk": sk}
                ),
                "ConditionExpression": "#field = :value",
                "ExpressionAttributeNames": {
                    "#field": field,
                },
                "ExpressionAttributeValues": self.registry._encode(
                    {":value": value}
                ),
            }
        }