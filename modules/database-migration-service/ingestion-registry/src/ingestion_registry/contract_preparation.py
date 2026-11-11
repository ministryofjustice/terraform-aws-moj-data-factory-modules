import copy
import hashlib
import json
import re
from dataclasses import asdict

from ingestion_registry.identity import TableIdentity


class ContractPreparationBlocked(ValueError):
    """Generation cannot use the selected deployment context."""


class ContractPreparation:
    def __init__(
        self,
        *,
        versions,
        generation_completion,
        deployment_binding: dict,
    ):
        if versions.registry is not generation_completion.registry:
            raise ValueError(
                "Version allocation and generation must use the same Registry"
            )

        if not isinstance(deployment_binding, dict):
            raise ValueError("deployment_binding must be an object")

        binding = copy.deepcopy(deployment_binding)

        try:
            table = TableIdentity(
                environment=binding["environment"],
                source=binding["source"],
                database_name=binding["database_name"],
                schema_name=binding["table"]["schema_name"],
                table_name=binding["table"]["table_name"],
            )

            account_id = binding["account_id"]
            region = binding["region"]

            if (
                not isinstance(account_id, str)
                or re.fullmatch(r"[0-9]{12}", account_id) is None
                or not isinstance(region, str)
                or re.fullmatch(r"[a-z0-9-]+", region) is None
            ):
                raise ValueError("Invalid deployment account or region")

            for field in (
                "configuration_revision",
                "artifact_revision",
            ):
                if (
                    not isinstance(binding[field], str)
                    or re.fullmatch(
                        r"[a-f0-9]{64}",
                        binding[field],
                    ) is None
                ):
                    raise ValueError(
                        f"{field} must contain a SHA256 revision"
                    )

            encoded_binding = json.dumps(
                binding,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                "Preparation requires a valid pinned table binding"
            ) from error

        if len(encoded_binding) > 65536:
            raise ValueError(
                "Deployment binding exceeds 64 KiB"
            )

        function_arn = generation_completion.generator.function_arn
        prefix = (
            f"arn:aws:lambda:{region}:{account_id}:function:"
        )

        if not function_arn.startswith(prefix):
            raise ValueError(
                "Generation capability must use the deployment "
                "account and region"
            )

        self.versions = versions
        self.completion = generation_completion
        self.registry = versions.registry
        self.table = table
        self.context = {
            "configuration_revision": binding["configuration_revision"],
            "artifact_revision": binding["artifact_revision"],
            "deployment_binding_sha256": hashlib.sha256(
                encoded_binding
            ).hexdigest(),
            "generation_function_arn": function_arn,
        }

    def prepare(
        self,
        *,
        table: TableIdentity,
        request_id: str,
    ) -> dict:
        if not isinstance(table, TableIdentity):
            raise ValueError("table must be a TableIdentity")

        if table != self.table:
            raise ContractPreparationBlocked(
                "Selected table does not match the pinned deployment binding"
            )

        if (
            not isinstance(request_id, str)
            or not request_id
            or request_id != request_id.strip()
            or len(request_id.encode("utf-8")) > 256
        ):
            raise ValueError(
                "request_id must contain 1 to 256 UTF-8 bytes "
                "without surrounding whitespace"
            )

        request = {
            "preparation_version": 1,
            "request_id": request_id,
            "table_identity": asdict(table),
            "generation_context": self.context,
        }

        fingerprint = hashlib.sha256(
            json.dumps(
                request,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

        reservation = self.versions.reserve(
            table=table,
            request_id=request_id,
            request_fingerprint=fingerprint,
        )

        generated = self.completion.complete(
            table=table,
            reservation_key=reservation["pk"],
            request_fingerprint=fingerprint,
        )

        if (
            generated.get("status") != "CANDIDATE_VERIFIED"
            or generated.get("request_id") != request_id
            or generated.get("reservation_key") != reservation["pk"]
            or generated.get("request_fingerprint") != fingerprint
            or generated.get("contract_version")
            != reservation["contract_version"]
            or generated.get("generation_function_arn")
            != self.context["generation_function_arn"]
        ):
            raise ContractPreparationBlocked(
                "Generation completion does not match its preparation request"
            )

        return {
            "status": "CANDIDATE_READY",
            "request_id": request_id,
            "table_identity": asdict(table),
            "reservation_key": reservation["pk"],
            "preparation_fingerprint": fingerprint,
            "contract_version": generated["contract_version"],
            "candidate_reference": copy.deepcopy(
                generated["candidate_reference"]
            ),
            "contract_fingerprint": generated["contract_fingerprint"],
            "generation_context": copy.deepcopy(self.context),
        }