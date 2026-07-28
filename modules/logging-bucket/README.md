## Data Factory Logging Bucket

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
| ---- | ------- |
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.7.0 |
| <a name="requirement_aws"></a> [aws](#requirement\_aws) | >= 6.0, < 7.0 |

## Providers

| Name | Version |
| ---- | ------- |
| <a name="provider_aws"></a> [aws](#provider\_aws) | >= 6.0, < 7.0 |

## Modules

| Name | Source | Version |
| ---- | ------ | ------- |
| <a name="module_log_bucket"></a> [log\_bucket](#module\_log\_bucket) | git::https://github.com/terraform-aws-modules/terraform-aws-s3-bucket.git | 0c0fb28347cc253088fe3966dca67420d39fbbe9 |

## Resources

| Name | Type |
| ---- | ---- |
| [aws_caller_identity.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/caller_identity) | data source |
| [aws_region.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/region) | data source |

## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| <a name="input_force_destroy"></a> [force\_destroy](#input\_force\_destroy) | Whether Terraform can delete the logging bucket when it contains objects. Keep false for production audit logs. | `bool` | `false` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| <a name="output_log_bucket_name"></a> [log\_bucket\_name](#output\_log\_bucket\_name) | The name of the central log bucket. |
<!-- END_TF_DOCS -->