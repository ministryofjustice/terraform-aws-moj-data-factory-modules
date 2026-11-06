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


def policy_acceptance_check(*, registry, verified: dict) -> dict:
    if verified.get("authorisation_kind") != "POLICY":
        raise ValueError("Expected verified policy authorisation")

    return {
        "ConditionCheck": {
            "TableName": registry.table_name,
            "Key": registry._encode(
                {
                    "pk": verified["request_key"],
                    "sk": "META",
                }
            ),
            "ConditionExpression": (
                "#status = :accepted "
                "AND #revision = :revision "
                "AND request_fingerprint = :fingerprint "
                "AND table_identity = :table "
                "AND request_id = :request "
                "AND assessment = :assessment "
                "AND #policy = :policy "
                "AND #decision = :decision"
            ),
            "ExpressionAttributeNames": {
                "#status": "status",
                "#revision": "revision",
                "#policy": "policy",
                "#decision": "decision",
            },
            "ExpressionAttributeValues": registry._encode(
                {
                    ":accepted": "POLICY_ACCEPTED",
                    ":revision": verified["expected_revision"],
                    ":fingerprint": verified["request_fingerprint"],
                    ":table": verified["table_identity"],
                    ":request": verified["request_id"],
                    ":assessment": verified["assessment"],
                    ":policy": verified["policy"],
                    ":decision": verified["decision"],
                }
            ),
        }
    }