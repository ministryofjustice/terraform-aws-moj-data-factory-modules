# Data Factory S3 Bucket

A generic bucket for landing raw data and integration with external systems.

## Usage
```hcl
module "data_factory_s3_bucket" {
    source = "github.com/ministryofjustice/terraform-aws-moj-data-factory-modules//modules/.."

    bucket_prefix = avature-landing
    kms_key_arn = module.bucket_kms_key.key_arn
    enable_malware_protection = true


    lifecycle_rule = [
        {
        id      = "main"
        enabled = "Enabled"
        prefix  = ""

        transition = [
            {
            days          = 90
            storage_class = "STANDARD_IA"
            },
            {
            days          = 365
            storage_class = "GLACIER"
            }
        ]

        expiration = {
            days = 730
        }
        }
    ]

    tags = {
    Project = 'Avature'
    Owner   = "CorporateDataEngineering"
    }    
}
```

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
| <a name="module_bucket"></a> [bucket](#module\_bucket) | git::https://github.com/terraform-aws-modules/terraform-aws-s3-bucket.git | 0c0fb28347cc253088fe3966dca67420d39fbbe9 |
| <a name="module_guardduty_scan_role"></a> [guardduty\_scan\_role](#module\_guardduty\_scan\_role) | git::https://github.com/terraform-aws-modules/terraform-aws-iam.git//modules/iam-role | 5b962b1163790398605f2b17447cf5b6cc512237 |

## Resources

| Name | Type |
| ---- | ---- |
| [aws_guardduty_malware_protection_plan.malware_protection_plan](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/guardduty_malware_protection_plan) | resource |
| [aws_caller_identity.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/caller_identity) | data source |
| [aws_region.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/region) | data source |

## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| <a name="input_bucket_prefix"></a> [bucket\_prefix](#input\_bucket\_prefix) | Prefix for the S3 bucket to create. | `string` | n/a | yes |
| <a name="input_enable_malware_protection"></a> [enable\_malware\_protection](#input\_enable\_malware\_protection) | Whether to enable GuardDuty malware protection for the bucket. | `bool` | `false` | no |
| <a name="input_force_destroy"></a> [force\_destroy](#input\_force\_destroy) | Whether Terraform can delete a non-empty bucket. Usually false outside dev. | `bool` | `false` | no |
| <a name="input_kms_key_arn"></a> [kms\_key\_arn](#input\_kms\_key\_arn) | ARN of the customer-managed KMS key used for S3 encryption. | `string` | n/a | yes |
| <a name="input_lifecycle_rules"></a> [lifecycle\_rules](#input\_lifecycle\_rules) | Optional lifecycle rules for the bucket. | `any` | `[]` | no |
| <a name="input_tags"></a> [tags](#input\_tags) | Tags to apply to created resources. | `map(string)` | `{}` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| <a name="output_bucket_arn"></a> [bucket\_arn](#output\_bucket\_arn) | ARN of the S3 bucket. |
| <a name="output_bucket_name"></a> [bucket\_name](#output\_bucket\_name) | Name of the S3 bucket. |
| <a name="output_guardduty_malware_protection_plan_id"></a> [guardduty\_malware\_protection\_plan\_id](#output\_guardduty\_malware\_protection\_plan\_id) | ID of the GuardDuty Malware Protection plan, if enabled. |
| <a name="output_guardduty_scan_role_arn"></a> [guardduty\_scan\_role\_arn](#output\_guardduty\_scan\_role\_arn) | ARN of the IAM role used by GuardDuty Malware Protection, if enabled. |
| <a name="output_guardduty_scan_role_name"></a> [guardduty\_scan\_role\_name](#output\_guardduty\_scan\_role\_name) | Name of the IAM role used by GuardDuty Malware Protection, if enabled. |
| <a name="output_kms_key_arn"></a> [kms\_key\_arn](#output\_kms\_key\_arn) | KMS key ARN used for bucket encryption. |
<!-- END_TF_DOCS -->