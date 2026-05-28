# Example Module

This module is a simple starter that demonstrates typical module structure and conventions for this repository.

## Usage

```hcl
module "example" {
  source = "github.com/ministryofjustice/terraform-aws-moj-data-factory-modules//modules/example?ref=v1.0.0"

  name        = "data-platform"
  environment = "dev"

  additional_tags = {
    Team = "data-engineering"
  }
}
```

## Notes

- This module does not create resources.
- It demonstrates variable validation, tag composition, and outputs.

## Inputs and Outputs

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
| ---- | ------- |
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.6.0 |

## Providers

No providers.

## Modules

No modules.

## Resources

No resources.

## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| <a name="input_additional_tags"></a> [additional\_tags](#input\_additional\_tags) | Extra tags to merge with the module defaults | `map(string)` | `{}` | no |
| <a name="input_environment"></a> [environment](#input\_environment) | Environment identifier (for example: dev, test, preprod, prod) | `string` | n/a | yes |
| <a name="input_name"></a> [name](#input\_name) | Name used for tagging and module outputs | `string` | n/a | yes |

## Outputs

| Name | Description |
| ---- | ----------- |
| <a name="output_environment"></a> [environment](#output\_environment) | Resolved environment value |
| <a name="output_name"></a> [name](#output\_name) | Resolved name value |
| <a name="output_tags"></a> [tags](#output\_tags) | Merged tag map |
<!-- END_TF_DOCS -->
