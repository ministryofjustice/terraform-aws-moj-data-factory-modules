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

- [example](modules/example/README.md): starter module that demonstrates structure, inputs, outputs, and tagging conventions

## Usage

Terraform modules from this repository can be consumed in downstream infrastructure code by referencing the module source and version.

Example:

```hcl
module "example" {
  source  = "github.com/ministryofjustice/terraform-aws-moj-data-factory-modules//modules/example?ref=v1.0.0"

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
