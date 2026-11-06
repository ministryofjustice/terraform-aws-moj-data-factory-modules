def human_approval_check(
    *,
    registry,
    request_key: str,
    request_fingerprint: str,
    revision: int,
    evidence_reference: dict,
) -> dict:
    return {
        "ConditionCheck": {
            "TableName": registry.table_name,
            "Key": registry._encode(
                {
                    "pk": request_key,
                    "sk": "META",
                }
            ),
            "ConditionExpression": (
                "#status = :approved "
                "AND #revision = :revision "
                "AND request_fingerprint = :fingerprint "
                "AND #review.#evidence = :evidence"
            ),
            "ExpressionAttributeNames": {
                "#status": "status",
                "#revision": "revision",
                "#review": "review",
                "#evidence": "evidence_reference",
            },
            "ExpressionAttributeValues": registry._encode(
                {
                    ":approved": "APPROVED",
                    ":revision": int(revision),
                    ":fingerprint": request_fingerprint,
                    ":evidence": evidence_reference,
                }
            ),
        }
    }