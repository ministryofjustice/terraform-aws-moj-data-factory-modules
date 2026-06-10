# Justice Data Factory Terraform Modules

[![Ministry of Justice Repository Compliance Badge](https://github-community.service.justice.gov.uk/repository-standards/api/terraform-aws-moj-data-factory-modules/badge)](https://github-community.service.justice.gov.uk/repository-standards/terraform-aws-moj-data-factory-modules)

This repository contains reusable Terraform modules for the Justice Data Factory platform.

These modules are intended to provide consistent, secure, and repeatable infrastructure patterns across Ministry of Justice data factories (the MoJ data mesh accounts).

## Purpose

The modules in this repository are designed to:

- standardise infrastructure across data factory environments
- reduce duplication by capturing common Terraform patterns once
- support secure-by-default implementation choices
- make it easier for teams to adopt shared, well-governed infrastructure

## Repository Structure

- `modules/` contains Terraform modules that can be consumed by data factory infrastructure repositories

As the repository grows, each module should include clear inputs, outputs, usage examples, and versioning guidance.

## Available Modules

<!-- prettier-ignore-start -->
<!-- markdownlint-disable -->
| Module | Description |
|--------|-------------|
| [airflow-iam-role](modules/airflow-iam-role/README.md) | Manages an AWS IAM role in a Justice Data Factory for use in Justice Data Platform Airflow workloads, including the trust policy and customer-managed IAM policies created from supplied policy documents and attached to the role. |
| [fabric-oidc-provider](modules/fabric-oidc-provider/README.md) | creates an AWS IAM OIDC provider for Microsoft Entra integration |
| [fabric-iam-role](modules/fabric-iam-role/README.md) | creates an IAM role for S3 access via Entra OIDC federation |
<!-- markdownlint-enable -->
<!-- prettier-ignore-end -->

## Runnable Examples

- [fabric_connector_test](examples/fabric_connector_test/README.md): end-to-end example wiring OIDC provider + IAM role with user-supplied values (run from within that directory)

## Usage

Terraform modules from this repository can be consumed in downstream infrastructure code by referencing the module source and version.

Example:

```hcl
module "example" {
  source  = "github.com/ministryofjustice/terraform-aws-moj-data-factory-modules//modules/<module name>?ref=<git hash>"

  # module inputs...
}
```

## Contributing

Contributions should follow standard MoJ engineering practices:

- keep modules small, composable, and well documented
- include sensible defaults and clear validation where possible
- update module documentation when inputs or outputs change
- ensure CI checks pass before merging

## License

This project is licensed under the terms of the [MIT License](LICENSE).

<!-- BEGIN_TF_DOCS -->
<!-- END_TF_DOCS -->
