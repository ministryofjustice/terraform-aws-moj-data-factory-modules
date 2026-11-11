import copy
import hashlib
import json
import re

from botocore.exceptions import ClientError

from contract_compatibility import assess_contracts


MAX_CONTRACT_BYTES = 1048576
MAX_EVIDENCE_BYTES = 65536

IDENTITY_FIELDS = {
    "source",
    "database_name",
    "schema_name",
    "table_name",
    "contract_version",
}

REFERENCE_FIELDS = {
    "bucket_name",
    "object_key",
    "version_id",
    "sha256",
}


class ContractAssessmentError(ValueError):
    """Assessment input, object content or stored evidence is inconsistent."""


def _unique_object(pairs):
    result = {}

    for key, value in pairs:
        if key in result:
            raise ContractAssessmentError(
                f"JSON contains a duplicate key: {key}"
            )
        result[key] = value

    return result


def _reject_constant(value):
    raise ContractAssessmentError(
        f"JSON contains an invalid constant: {value}"
    )


def _encode(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _decode(raw):
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractAssessmentError(
            "Object is not valid UTF-8 JSON"
        ) from error


def _text(value, *, max_bytes):
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value.encode("utf-8")) <= max_bytes
    )


def _version(value):
    return (
        isinstance(value, str)
        and re.fullmatch(r"v[1-9][0-9]*", value) is not None
    )


def _location(value, *, evidence=False):
    expected = {
        "bucket_name",
        "prefix",
        "bucket_owner_account_id",
    }

    if evidence:
        expected.add("kms_key_arn")

    if not isinstance(value, dict) or set(value) != expected:
        raise ContractAssessmentError(
            "Assessment location has an unsupported structure"
        )

    bucket = value["bucket_name"]
    prefix = value["prefix"]
    owner = value["bucket_owner_account_id"]

    if (
        not isinstance(bucket, str)
        or re.fullmatch(
            r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]",
            bucket,
        ) is None
        or not _text(prefix, max_bytes=1024)
        or not prefix.endswith("/")
        or any(
            not segment
            or segment in {".", ".."}
            or segment != segment.strip()
            or "*" in segment
            or "?" in segment
            for segment in prefix[:-1].split("/")
        )
        or not isinstance(owner, str)
        or re.fullmatch(r"[0-9]{12}", owner) is None
    ):
        raise ContractAssessmentError(
            "Assessment location is invalid"
        )

    if evidence:
        _kms_key(value["kms_key_arn"])


def _kms_key(value):
    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"arn:aws:kms:[a-z0-9-]+:[0-9]{12}:key/"
            r"[A-Za-z0-9-]+",
            value,
        ) is None
    ):
        raise ContractAssessmentError(
            "Assessment encryption requires an explicit KMS key ARN"
        )


class ContractAssessmentService:
    def __init__(
        self,
        *,
        s3_client,
        configuration,
        validate_contract,
        canonicalise_schema,
    ):
        required = {
            "assessor_revision",
            "candidate",
            "baseline",
            "evidence",
            "read_kms_key_arns",
        }

        if (
            not isinstance(configuration, dict)
            or set(configuration) != required
            or not _text(
                configuration["assessor_revision"],
                max_bytes=256,
            )
        ):
            raise ContractAssessmentError(
                "Assessment configuration is invalid"
            )

        _location(configuration["candidate"])
        _location(configuration["baseline"])
        _location(configuration["evidence"], evidence=True)

        keys = configuration["read_kms_key_arns"]

        if not isinstance(keys, (list, tuple, set, frozenset)):
            raise ContractAssessmentError(
                "read_kms_key_arns must be a collection of KMS key ARNs"
            )

        for key in keys:
            _kms_key(key)

        if not callable(validate_contract) or not callable(
            canonicalise_schema
        ):
            raise ValueError(
                "Validation and canonicalisation callbacks are required"
            )

        self.s3 = s3_client
        self.configuration = copy.deepcopy(configuration)
        self.validate_contract = validate_contract
        self.canonicalise_schema = canonicalise_schema

    def _check_request(self, request):
        required = {
            "assessment_request_version",
            "request_id",
            "identity",
            "candidate_reference",
            "baseline_reference",
            "assessor_revision",
        }

        if (
            not isinstance(request, dict)
            or set(request) != required
            or type(request["assessment_request_version"]) is not int
            or request["assessment_request_version"] != 1
            or not _text(request["request_id"], max_bytes=256)
            or request["assessor_revision"]
            != self.configuration["assessor_revision"]
        ):
            raise ContractAssessmentError(
                "Assessment request has an invalid structure or revision"
            )

        identity = request["identity"]

        if (
            not isinstance(identity, dict)
            or set(identity) != IDENTITY_FIELDS
            or not isinstance(identity["source"], str)
            or re.fullmatch(
                r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*",
                identity["source"],
            ) is None
            or len(identity["source"]) > 63
            or not _version(identity["contract_version"])
            or any(
                not isinstance(identity[field], str)
                or re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_$#]{0,127}",
                    identity[field],
                ) is None
                for field in (
                    "database_name",
                    "schema_name",
                    "table_name",
                )
            )
        ):
            raise ContractAssessmentError(
                "Assessment request has an invalid contract identity"
            )

        if len(_encode(request)) > MAX_EVIDENCE_BYTES:
            raise ContractAssessmentError(
                "Assessment request exceeds 64 KiB"
            )

    def _read_contract(self, reference, location):
        if (
            not isinstance(reference, dict)
            or set(reference) != REFERENCE_FIELDS
            or reference["bucket_name"] != location["bucket_name"]
            or not _text(reference["object_key"], max_bytes=1024)
            or not reference["object_key"].startswith(
                location["prefix"]
            )
            or not _text(reference["version_id"], max_bytes=1024)
            or reference["version_id"] == "null"
            or not isinstance(reference["sha256"], str)
            or re.fullmatch(
                r"[a-f0-9]{64}",
                reference["sha256"],
            ) is None
        ):
            raise ContractAssessmentError(
                "Contract reference is outside its trusted location "
                "or is invalid"
            )

        response = self.s3.get_object(
            Bucket=reference["bucket_name"],
            Key=reference["object_key"],
            VersionId=reference["version_id"],
            ExpectedBucketOwner=location["bucket_owner_account_id"],
        )
        body = response["Body"]

        try:
            if response.get("VersionId") != reference["version_id"]:
                raise ContractAssessmentError(
                    "S3 returned a different contract object version"
                )

            raw = body.read(MAX_CONTRACT_BYTES + 1)
        finally:
            body.close()

        if len(raw) > MAX_CONTRACT_BYTES:
            raise ContractAssessmentError(
                "Contract exceeds 1 MiB"
            )

        if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            raise ContractAssessmentError(
                "Contract checksum does not match its reference"
            )

        document = _decode(raw)

        if not isinstance(document, dict):
            raise ContractAssessmentError(
                "Contract must be a JSON object"
            )

        return document

    @staticmethod
    def _check_identity(document, identity, *, baseline=False):
        fields = IDENTITY_FIELDS - {"contract_version"}

        if not baseline:
            fields = IDENTITY_FIELDS

        if any(
            document.get(field) != identity[field]
            for field in fields
        ):
            raise ContractAssessmentError(
                "Contract identity does not match the selected table"
            )

        if not _version(document.get("contract_version")):
            raise ContractAssessmentError(
                "Contract version is invalid"
            )

    def assess(self, *, request):
        request = copy.deepcopy(request)
        self._check_request(request)

        candidate = self._read_contract(
            request["candidate_reference"],
            self.configuration["candidate"],
        )
        self._check_identity(candidate, request["identity"])

        baseline = None

        if request["baseline_reference"] is not None:
            baseline = self._read_contract(
                request["baseline_reference"],
                self.configuration["baseline"],
            )
            self._check_identity(
                baseline,
                request["identity"],
                baseline=True,
            )

            expected_key = "/".join(
                [
                    request["identity"]["source"],
                    request["identity"]["database_name"],
                    request["identity"]["schema_name"],
                    request["identity"]["table_name"],
                    baseline["contract_version"],
                    "contract.avsc",
                ]
            )

            if request["baseline_reference"]["object_key"] != expected_key:
                raise ContractAssessmentError(
                    "Baseline object key does not match its contract identity"
                )

        outcome = assess_contracts(
            candidate=candidate,
            baseline=baseline,
            validate_contract=self.validate_contract,
            canonicalise_schema=self.canonicalise_schema,
        )

        evidence = {
            "validation_result_version": 1,
            **{
                field: request[field]
                for field in (
                    "request_id",
                    "identity",
                    "candidate_reference",
                    "baseline_reference",
                    "assessor_revision",
                )
            },
            "validation": outcome["validation"],
            "compatibility": outcome["compatibility"],
        }

        reference = self._store_evidence(
            request=request,
            evidence=evidence,
        )

        return {
            "status": "ASSESSMENT_COMPLETED",
            "request_id": request["request_id"],
            "evidence_reference": reference,
            "validation": outcome["validation"],
            "compatibility": outcome["compatibility"],
            "reason": outcome["reason"],
            "ruleset_revision": outcome["ruleset_revision"],
        }

    def _store_evidence(self, *, request, evidence):
        location = self.configuration["evidence"]
        content = _encode(evidence)

        if len(content) > MAX_EVIDENCE_BYTES:
            raise ContractAssessmentError(
                "Assessment evidence exceeds 64 KiB"
            )

        digest = hashlib.sha256(_encode(request)).hexdigest()
        key = f"{location['prefix']}{digest}/result.json"

        try:
            response = self.s3.put_object(
                Bucket=location["bucket_name"],
                Key=key,
                Body=content,
                ContentType="application/json",
                IfNoneMatch="*",
                ExpectedBucketOwner=location["bucket_owner_account_id"],
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=location["kms_key_arn"],
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            status = error.response.get(
                "ResponseMetadata", {}
            ).get("HTTPStatusCode")

            if code not in {"PreconditionFailed", "412"} and status != 412:
                raise

            version_id = None
        else:
            version_id = response.get("VersionId")

        read_request = {
            "Bucket": location["bucket_name"],
            "Key": key,
            "ExpectedBucketOwner": location["bucket_owner_account_id"],
        }

        if version_id is not None:
            read_request["VersionId"] = version_id

        response = self.s3.get_object(**read_request)
        body = response["Body"]

        try:
            actual_version = response.get("VersionId")

            if (
                not _text(actual_version, max_bytes=1024)
                or actual_version == "null"
                or (
                    version_id is not None
                    and actual_version != version_id
                )
            ):
                raise ContractAssessmentError(
                    "Evidence storage requires an exact versioned object"
                )

            if (
                response.get("ServerSideEncryption") != "aws:kms"
                or response.get("SSEKMSKeyId")
                != location["kms_key_arn"]
            ):
                raise ContractAssessmentError(
                    "Evidence encryption does not match configuration"
                )

            actual = body.read(len(content) + 1)
        finally:
            body.close()

        if actual != content:
            raise ContractAssessmentError(
                "Assessment request already has different evidence; "
                "use a new request or assessor revision"
            )

        return {
            "bucket_name": location["bucket_name"],
            "object_key": key,
            "version_id": actual_version,
            "sha256": hashlib.sha256(actual).hexdigest(),
        }