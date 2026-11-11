import copy
import re
from dataclasses import asdict

from botocore.exceptions import ClientError

from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference


class ContractGenerationConflict(ValueError):
    """Generation completion conflicts with its reservation or evidence."""


class ContractGenerationCompletion:
    def __init__(
        self,
        *,
        generator,
        candidate_kms_key_arn: str,
    ):
        if (
            not isinstance(candidate_kms_key_arn, str)
            or re.fullmatch(
                r"arn:aws:kms:[a-z0-9-]+:[0-9]{12}:key/"
                r"[A-Za-z0-9-]+",
                candidate_kms_key_arn,
            ) is None
        ):
            raise ValueError(
                "candidate_kms_key_arn must be an explicit KMS key ARN"
            )

        self.generator = generator
        self.registry = generator.registry
        self.reader = generator.candidate_reader
        self.kms_key_arn = candidate_kms_key_arn

    def complete(
        self,
        *,
        table: TableIdentity,
        reservation_key: str,
        request_fingerprint: str,
    ) -> dict:
        reservation = self._reservation(
            table,
            reservation_key,
            request_fingerprint,
        )

        existing = self.registry._read(
            reservation_key,
            "GENERATION",
        )

        if existing is not None:
            result = self._verify_record(
                existing,
                table,
                reservation,
            )
        else:
            recovered = self.reader.recover(
                table=table,
                contract_version=reservation["contract_version"],
                request_id=reservation["request_id"],
                kms_key_arn=self.kms_key_arn,
            )

            if recovered is None:
                result = self.generator.generate(
                    table=table,
                    reservation_key=reservation_key,
                    request_fingerprint=request_fingerprint,
                )
            else:
                result = self._result(
                    table,
                    reservation,
                    recovered,
                )

        record = {
            "pk": reservation_key,
            "sk": "GENERATION",
            "record_type": "CONTRACT_GENERATION",
            "result": copy.deepcopy(result),
        }

        for _ in range(3):
            existing = self.registry._read(
                reservation_key,
                "GENERATION",
            )

            if existing is None:
                completion_action = self.registry._put_new(record)
            else:
                existing_result = self._verify_record(
                    existing,
                    table,
                    reservation,
                )

                if existing_result != result:
                    raise ContractGenerationConflict(
                        "Generation already completed with different evidence"
                    )

                completion_action = {
                    "ConditionCheck": {
                        "TableName": self.registry.table_name,
                        "Key": self.registry._encode(
                            {
                                "pk": reservation_key,
                                "sk": "GENERATION",
                            }
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
                                ":kind": "CONTRACT_GENERATION",
                                ":result": result,
                            }
                        ),
                    }
                }

            try:
                self.registry.client.transact_write_items(
                    TransactItems=[
                        self._reservation_check(reservation),
                        completion_action,
                    ],
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

                current = self._reservation(
                    table,
                    reservation_key,
                    request_fingerprint,
                )

                if current != reservation:
                    raise ContractGenerationConflict(
                        "Version reservation changed during completion"
                    ) from error

                continue

            return copy.deepcopy(result)

        raise ContractGenerationConflict(
            "Generation completion encountered concurrent changes; "
            "retry using the same reservation"
        )

    def _reservation(
        self,
        table,
        reservation_key,
        request_fingerprint,
    ):
        if not isinstance(table, TableIdentity):
            raise ValueError("table must be a TableIdentity")

        if (
            not isinstance(reservation_key, str)
            or re.fullmatch(
                r"CONTRACTVERSIONREQUEST#[a-f0-9]{64}",
                reservation_key,
            ) is None
        ):
            raise ValueError("Invalid contract version reservation key")

        if (
            not isinstance(request_fingerprint, str)
            or re.fullmatch(
                r"[a-f0-9]{64}",
                request_fingerprint,
            ) is None
        ):
            raise ValueError("Invalid request fingerprint")

        reservation = self.registry._read(reservation_key, "META")

        if (
            not isinstance(reservation, dict)
            or reservation.get("pk") != reservation_key
            or reservation.get("sk") != "META"
            or reservation.get("record_type")
            != "CONTRACT_VERSION_RESERVATION"
            or reservation.get("table_identity") != asdict(table)
            or reservation.get("request_fingerprint")
            != request_fingerprint
        ):
            raise ContractGenerationConflict(
                "Version reservation does not match the generation request"
            )

        request_id = reservation.get("request_id")
        version = reservation.get("contract_version")

        if (
            not isinstance(request_id, str)
            or not request_id
            or request_id != request_id.strip()
            or len(request_id.encode("utf-8")) > 256
            or not isinstance(version, str)
            or re.fullmatch(r"v[1-9][0-9]*", version) is None
        ):
            raise ContractGenerationConflict(
                "Version reservation contains invalid request information"
            )

        return reservation

    def _result(self, table, reservation, candidate):
        return {
            "status": "CANDIDATE_VERIFIED",
            "request_id": reservation["request_id"],
            "reservation_key": reservation["pk"],
            "request_fingerprint": reservation["request_fingerprint"],
            "generation_function_arn": self.generator.function_arn,
            "source": table.source,
            "database_name": table.database_name,
            "schema_name": table.schema_name,
            "table_name": table.table_name,
            "contract_version": reservation["contract_version"],
            "candidate_reference": asdict(candidate.reference),
            "contract_fingerprint": candidate.contract_fingerprint,
        }

    def _verify_record(self, record, table, reservation):
        result = record.get("result")

        if (
            record.get("pk") != reservation["pk"]
            or record.get("sk") != "GENERATION"
            or record.get("record_type") != "CONTRACT_GENERATION"
            or not isinstance(result, dict)
        ):
            raise ContractGenerationConflict(
                "Stored generation completion is invalid"
            )

        reference_data = result.get("candidate_reference")

        if (
            not isinstance(reference_data, dict)
            or set(reference_data)
            != {"bucket_name", "object_key", "version_id", "sha256"}
        ):
            raise ContractGenerationConflict(
                "Stored generation candidate reference is invalid"
            )

        try:
            reference = S3ObjectReference(**reference_data)
        except ValueError as error:
            raise ContractGenerationConflict(
                "Stored generation candidate reference is invalid"
            ) from error

        verified = self.reader.read(
            table=table,
            contract_version=reservation["contract_version"],
            reference=reference,
        )

        expected = self._result(table, reservation, verified)

        if result != expected:
            raise ContractGenerationConflict(
                "Stored generation completion does not match "
                "the reservation, deployed function or candidate"
            )

        return copy.deepcopy(expected)

    def _reservation_check(self, reservation):
        return {
            "ConditionCheck": {
                "TableName": self.registry.table_name,
                "Key": self.registry._encode(
                    {
                        "pk": reservation["pk"],
                        "sk": "META",
                    }
                ),
                "ConditionExpression": (
                    "#kind = :kind "
                    "AND table_identity = :table "
                    "AND request_id = :request "
                    "AND request_fingerprint = :fingerprint "
                    "AND contract_version = :version"
                ),
                "ExpressionAttributeNames": {
                    "#kind": "record_type",
                },
                "ExpressionAttributeValues": self.registry._encode(
                    {
                        ":kind": "CONTRACT_VERSION_RESERVATION",
                        ":table": reservation["table_identity"],
                        ":request": reservation["request_id"],
                        ":fingerprint": reservation["request_fingerprint"],
                        ":version": reservation["contract_version"],
                    }
                ),
            }
        }