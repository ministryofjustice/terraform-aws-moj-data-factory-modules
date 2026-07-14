# Data Factory S3 Bucket
A generic bucket for landing raw data and integration with external systems.

<!-- BEGIN_TF_DOCS -->

## Requirements

| Name                                                                     | Version  |
| ------------------------------------------------------------------------ | -------- |
| <a name="requirement_terraform"></a> [terraform](#requirement_terraform) | >= 1.7.0 |
| <a name="requirement_aws"></a> [aws](#requirement_aws)                   | >= 6.0   |

## Providers

| Name                                             | Version |
| ------------------------------------------------ | ------- |
| <a name="provider_aws"></a> [aws](#provider_aws) | >= 6.0  |

## Modules

| Name                                                  | Source                                                                    | Version                                  |
| ----------------------------------------------------- | ------------------------------------------------------------------------- | ---------------------------------------- |
| <a name="module_bucket"></a> [bucket](#module_bucket) | git::https://github.com/terraform-aws-modules/terraform-aws-s3-bucket.git | 0c0fb28347cc253088fe3966dca67420d39fbbe9 |

## Resources

| Name                                                                                                                          | Type        |
| ----------------------------------------------------------------------------------------------------------------------------- | ----------- |
| [aws_caller_identity.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/caller_identity) | data source |
| [aws_region.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/region)                   | data source |

## Inputs

| Name                                                                           | Description                                                                 | Type          | Default | Required |
| ------------------------------------------------------------------------------ | --------------------------------------------------------------------------- | ------------- | ------- | :------: |
| <a name="input_bucket_prefix"></a> [bucket_prefix](#input_bucket_prefix)       | Prefix for the S3 bucket to create.                                         | `string`      | n/a     |   yes    |
| <a name="input_force_destroy"></a> [force_destroy](#input_force_destroy)       | Whether Terraform can delete a non-empty bucket. Usually false outside dev. | `bool`        | `false` |    no    |
| <a name="input_kms_key_arn"></a> [kms_key_arn](#input_kms_key_arn)             | ARN of the customer-managed KMS key used for S3 encryption.                 | `string`      | n/a     |   yes    |
| <a name="input_lifecycle_rules"></a> [lifecycle_rules](#input_lifecycle_rules) | Optional lifecycle rules for the bucket.                                    | `any`         | `[]`    |    no    |
| <a name="input_tags"></a> [tags](#input_tags)                                  | Tags to apply to created resources.                                         | `map(string)` | `{}`    |    no    |

## Outputs

| Name                                                                 | Description            |
| -------------------------------------------------------------------- | ---------------------- |
| <a name="output_bucket_arn"></a> [bucket_arn](#output_bucket_arn)    | ARN of the S3 bucket.  |
| <a name="output_bucket_name"></a> [bucket_name](#output_bucket_name) | Name of the S3 bucket. |

<!-- END_TF_DOCS -->
