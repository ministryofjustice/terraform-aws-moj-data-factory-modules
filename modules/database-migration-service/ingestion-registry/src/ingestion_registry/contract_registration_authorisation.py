import copy


def authorise_registration(
    *,
    approval_gate,
    policy_authorisation,
    table,
    request_id,
    contract_version,
):
    if policy_authorisation is not None:
        if (
            policy_authorisation.registry
            is not approval_gate.lifecycle.registry
        ):
            raise ValueError(
                "Human and policy authorisation must use the same Registry"
            )

        request = policy_authorisation.lifecycle.read(
            table=table,
            request_id=request_id,
        )

        if request is not None and request.get("status") == "POLICY_ACCEPTED":
            record = policy_authorisation.authorise_registration(
                table=table,
                request_id=request_id,
                contract_version=contract_version,
            )

            return {
                "kind": "POLICY",
                "request_key": record["pk"],
                "request_fingerprint": record["request_fingerprint"],
                "revision": int(record["authorisation_revision"]),
                "evidence_reference": copy.deepcopy(
                    record["authorisation_evidence_reference"]
                ),
                "candidate_reference": copy.deepcopy(
                    record["assessment"]["candidate_reference"]
                ),
                "policy": copy.deepcopy(record["policy"]),
                "contract_version": record["contract_version"],
            }

    record = approval_gate.authorise_registration(
        table=table,
        request_id=request_id,
    )

    return {
        "kind": "HUMAN",
        "request_key": record["pk"],
        "request_fingerprint": record["request_fingerprint"],
        "revision": int(record["approval_revision"]),
        "evidence_reference": copy.deepcopy(
            record["approval_reference"]
        ),
        "candidate_reference": copy.deepcopy(
            record["assessment"]["candidate_reference"]
        ),
    }


def authorisation_fields(authorisation):
    common = {
        "request_key": authorisation["request_key"],
        "request_fingerprint": authorisation["request_fingerprint"],
        "candidate_reference": copy.deepcopy(
            authorisation["candidate_reference"]
        ),
    }

    if authorisation["kind"] == "HUMAN":
        return {
            **common,
            "approval_revision": authorisation["revision"],
            "approval_reference": copy.deepcopy(
                authorisation["evidence_reference"]
            ),
        }

    if authorisation["kind"] == "POLICY":
        return {
            **common,
            "authorisation": copy.deepcopy(authorisation),
        }

    raise ValueError("Unsupported contract authorisation kind")