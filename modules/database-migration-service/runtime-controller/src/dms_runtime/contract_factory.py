from dataclasses import dataclass

from ingestion_config.published_resolver import resolve_published_table

from ingestion_registry.contract_activation import ContractActivation
from ingestion_registry.contract_approval import ContractApprovalGate
from ingestion_registry.contract_assessment import ContractAssessmentAdmission
from ingestion_registry.contract_assessment_requests import (
    ContractAssessmentRequests,
)
from ingestion_registry.contract_candidate import ContractCandidateReader
from ingestion_registry.contract_generation import ContractGenerator
from ingestion_registry.contract_generation_assessment import (
    GeneratedContractAssessment,
)
from ingestion_registry.contract_generation_completion import (
    ContractGenerationCompletion,
)
from ingestion_registry.contract_lifecycle import ContractLifecycle
from ingestion_registry.contract_lookup import CurrentContractLookup
from ingestion_registry.contract_orchestration import ContractOrchestration
from ingestion_registry.contract_pinning import CurrentContractPinning
from ingestion_registry.contract_policy import ContractPolicy
from ingestion_registry.contract_policy_authorisation import (
    ContractPolicyAuthorisation,
)
from ingestion_registry.contract_preparation import ContractPreparation
from ingestion_registry.contract_promotion import ContractPromoter
from ingestion_registry.contract_registration import ContractRegistrar
from ingestion_registry.contract_registration_completion import (
    ContractRegistrationCompletion,
)
from ingestion_registry.contract_reuse import ContractReuse
from ingestion_registry.contract_validation import ContractValidator
from ingestion_registry.contract_verification import RegisteredContractVerifier
from ingestion_registry.contract_versions import ContractVersions
from ingestion_registry.contracts import IngestionContracts
from ingestion_registry.identity import TableIdentity
from ingestion_registry.progress import IngestionProgress


@dataclass(frozen=True)
class ContractObjectLocation:
    bucket_name: str
    prefix: str
    bucket_owner_account_id: str


@dataclass(frozen=True)
class ContractRuntimeSettings:
    candidate: ContractObjectLocation
    approval: ContractObjectLocation
    evidence: ContractObjectLocation
    approved_bucket_name: str
    approved_bucket_owner_account_id: str
    candidate_kms_key_arn: str
    approved_kms_key_arn: str
    assessor_revision: str
    policy: ContractPolicy


@dataclass(frozen=True)
class ContractRuntime:
    table: TableIdentity
    orchestration: ContractOrchestration
    lifecycle: ContractLifecycle
    pinning: CurrentContractPinning


def build_contract_runtime(
    *,
    registry,
    document_client,
    dynamodb_resource,
    lambda_client,
    s3_client,
    manifest_reference,
    artifact_reference,
    catalog_reference,
    allowed_bucket: str,
    allowed_prefix: str,
    expected_bucket_owner: str,
    lookup,
    settings: ContractRuntimeSettings,
    authorise,
) -> ContractRuntime:
    if not isinstance(settings, ContractRuntimeSettings):
        raise ValueError(
            "settings must be ContractRuntimeSettings"
        )

    for location in (
        settings.candidate,
        settings.approval,
        settings.evidence,
    ):
        if not isinstance(location, ContractObjectLocation):
            raise ValueError(
                "Contract locations must be ContractObjectLocation"
            )

    if not isinstance(settings.policy, ContractPolicy):
        raise ValueError("A trusted ContractPolicy is required")

    if not callable(authorise):
        raise ValueError(
            "A trusted reviewer authorisation callback is required"
        )

    if (
        registry.client.meta.region_name != lookup.region
        or lambda_client.meta.region_name != lookup.region
        or dynamodb_resource.meta.client.meta.region_name != lookup.region
    ):
        raise ValueError(
            "Registry, DynamoDB and Lambda clients must use "
            "the selected deployment region"
        )

    binding = resolve_published_table(
        document_client,
        manifest_reference=manifest_reference,
        artifact_reference=artifact_reference,
        catalog_reference=catalog_reference,
        allowed_bucket=allowed_bucket,
        allowed_prefix=allowed_prefix,
        expected_bucket_owner=expected_bucket_owner,
        lookup=lookup,
        required_contract_capabilities=(
            "generation",
            "validation",
            "registration",
            "registry",
        ),
    )

    capabilities = binding["contract_bindings"]

    table = TableIdentity(
        environment=binding["environment"],
        source=binding["source"],
        database_name=binding["database_name"],
        schema_name=binding["table"]["schema_name"],
        table_name=binding["table"]["table_name"],
    )

    registration_table = dynamodb_resource.Table(
        capabilities["registry"]["table_name"]
    )

    lifecycle = ContractLifecycle(registry)

    approval_gate = ContractApprovalGate(
        lifecycle=lifecycle,
        s3_client=s3_client,
        approval_bucket=settings.approval.bucket_name,
        approval_prefix=settings.approval.prefix,
        bucket_owner_account_id=(
            settings.approval.bucket_owner_account_id
        ),
        authorise=authorise,
    )

    candidate_reader = ContractCandidateReader(
        s3_client=s3_client,
        candidate_bucket=settings.candidate.bucket_name,
        candidate_prefix=settings.candidate.prefix,
        bucket_owner_account_id=(
            settings.candidate.bucket_owner_account_id
        ),
    )

    verifier = RegisteredContractVerifier(
        contract_table=registration_table,
        s3_client=s3_client,
        bucket_name=settings.approved_bucket_name,
        bucket_owner_account_id=(
            settings.approved_bucket_owner_account_id
        ),
    )

    current_lookup = CurrentContractLookup(
        registry=registry,
        approval_gate=approval_gate,
        verifier=verifier,
    )

    admission = ContractAssessmentAdmission(
        lifecycle=lifecycle,
        candidate_reader=candidate_reader,
        current_lookup=current_lookup,
        s3_client=s3_client,
        evidence_bucket=settings.evidence.bucket_name,
        evidence_prefix=settings.evidence.prefix,
        bucket_owner_account_id=(
            settings.evidence.bucket_owner_account_id
        ),
        assessor_revision=settings.assessor_revision,
    )

    policy_authorisation = ContractPolicyAuthorisation(
        assessment_admission=admission,
        trusted_policy=settings.policy,
    )

    current_lookup.policy_authorisation = policy_authorisation

    promoter = ContractPromoter(
        approval_gate=approval_gate,
        policy_authorisation=policy_authorisation,
        candidate_reader=candidate_reader,
        s3_client=s3_client,
        destination_bucket=settings.approved_bucket_name,
        bucket_owner_account_id=(
            settings.approved_bucket_owner_account_id
        ),
        kms_key_arn=settings.approved_kms_key_arn,
    )

    registrar = ContractRegistrar(
        promoter=promoter,
        approval_gate=approval_gate,
        policy_authorisation=policy_authorisation,
        verifier=verifier,
        lambda_client=lambda_client,
        registration_function_arn=(
            capabilities["registration"]["arn"]
        ),
    )

    registration_completion = ContractRegistrationCompletion(
        registrar=registrar,
        approval_gate=approval_gate,
        policy_authorisation=policy_authorisation,
    )

    activation = ContractActivation(
        completion=registration_completion,
        lifecycle=lifecycle,
    )

    versions = ContractVersions(
        registry=registry,
        registration_table=registration_table,
    )

    generator = ContractGenerator(
        registry=registry,
        candidate_reader=candidate_reader,
        lambda_client=lambda_client,
        generation_function_arn=capabilities["generation"]["arn"],
    )

    generation_completion = ContractGenerationCompletion(
        generator=generator,
        candidate_kms_key_arn=settings.candidate_kms_key_arn,
    )

    preparation = ContractPreparation(
        versions=versions,
        generation_completion=generation_completion,
        deployment_binding=binding,
    )

    generated_assessment = GeneratedContractAssessment(
        generation_completion=generation_completion,
        assessment_admission=admission,
    )

    assessment_requests = ContractAssessmentRequests(
        generation_completion=generation_completion,
        current_lookup=current_lookup,
        assessor_revision=settings.assessor_revision,
    )

    validator = ContractValidator(
        assessment_requests=assessment_requests,
        lambda_client=lambda_client,
        validation_function_arn=capabilities["validation"]["arn"],
    )

    orchestration = ContractOrchestration(
        lifecycle=lifecycle,
        lookup=current_lookup,
        assessment_admission=admission,
        reuse=ContractReuse(
            lifecycle=lifecycle,
            current_lookup=current_lookup,
            contract_verifier=verifier,
        ),
        activation=activation,
        trusted_policy=settings.policy,
        preparation=preparation,
        generated_assessment=generated_assessment,
        assessment_requests=assessment_requests,
        validator=validator,
    )

    pinning = CurrentContractPinning(
        lookup=current_lookup,
        contracts=IngestionContracts(
            IngestionProgress(registry)
        ),
    )

    return ContractRuntime(
        table=table,
        orchestration=orchestration,
        lifecycle=lifecycle,
        pinning=pinning,
    )