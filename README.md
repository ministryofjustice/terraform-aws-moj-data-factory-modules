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
| [airflow-iam-role](modules/airflow-iam-role/README.md) | Manages an AWS IAM role in a Justice Data Factory for use in Justice Data Platform Airflow workloads, including trust policy, optional managed policies, and optional inline policies. |
<!-- markdownlint-enable -->
<!-- prettier-ignore-end -->

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
