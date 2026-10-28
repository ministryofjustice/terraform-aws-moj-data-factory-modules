# Configured ingestion

This module connects service-ingestion and dms-runtime-controller.

Each configured service gets its DMS resources and a runtime Lambda.
The Lambda receives permissions for that service's deployed DMS resources.

Adding a service means adding configuration and its platform-owned runtime
settings, rather than copying another Terraform implementation.

This currently supports development in eu-west-2 only.

## What it needs

- Compiled service configurations
- A published runtime ZIP, pinned to its S3 version and checksum
- The existing Ingestion Registry
- Each service's published-document location
- Each source's credential preparation settings
- Tags and log retention

The runtime settings must have exactly the same service keys as the
compiled configurations.

## Deployment outputs

The deployments output contains the compiler artifacts, actual DMS
resource bindings and runtime Lambda references.

After apply:

    terraform output -json deployments > deployments.json

Prepare and validate the service deployment documents:

    ingestion-config build-deployment-bundle --deployments deployments.json --account-id <account-id> --output deployment-bundle.json

This prepares documents. It does not upload them or admit an ingestion.

## Boundaries

This module does not create source databases, approve contracts or start
DMS tasks. Shared workflow deployment is still a separate integration.

Each task group is explicitly assigned to platform-owned replication
capacity. The composition does not create an instance per service by default.

Endpoints, tasks and landing prefixes remain separate for each extraction
generation. The runtime Lambda remains shared within its service.

Prepared generations are retained in the compiled inventory. Ordinary changes
that remove a generation, change its artifact or move it to another instance
are blocked by a Terraform lifecycle guard. Retirement needs a separate
reviewed procedure.

Pool placement does not perform account quota checks or runtime workload
reservation. Those remain separate deployment and admission controls.

## Checks

The Terraform tests plan PostgreSQL and Oracle through the same module
using a mock provider. They do not prove a live AWS deployment.