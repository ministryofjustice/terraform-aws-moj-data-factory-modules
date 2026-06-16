<!-- BEGIN_TF_DOCS -->
# Lake Formation Database
This module creates an S3 location registered in AWS Lake Formation and an AWS Glue Catalog Database backed by that S3 bucket.

## Usage
```hcl
module "lakeformation_database" {
  source = "github.com/ministryofjustice/terraform-aws-moj-data-factory-modules//modules/lakeformation-database?ref=<git-sha>"

  database_name = "example"

  storage = {
    bucket_name = <bucket_name>
    prefix      = "example"
    kms_key_arn = "arn:aws:kms:eu-west-2:1234567890:key/example"
  }
  tags          = local.tags
}
```

## Requirements

| Name | Version |
|------|---------|
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.5 |
| <a name="requirement_aws"></a> [aws](#requirement\_aws) | ~> 6.0 |

## Providers

| Name | Version |
|------|---------|
| <a name="provider_aws"></a> [aws](#provider\_aws) | ~> 6.0 |

## Modules

No modules.

## Resources

| Name | Type |
|------|------|
| [aws_glue_catalog_database.data_factory_database](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/glue_catalog_database) | resource |
| [aws_iam_role.lakeformation_s3_access_role](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role_policy.lakeformation_s3_access_policy](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_lakeformation_resource.s3_location](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lakeformation_resource) | resource |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_database_name"></a> [database\_name](#input\_database\_name) | The name of the Glue catalog database to create. | `string` | n/a | yes |
| <a name="input_storage"></a> [storage](#input\_storage) | n/a | <pre>object(<br/>    {<br/>      bucket_name = string<br/>      prefix      = string<br/>      kms_key_arn = string<br/>    }<br/>  )</pre> | n/a | yes |
| <a name="input_tags"></a> [tags](#input\_tags) | Tags to apply to created resources. | `map(string)` | `{}` | no |

## Outputs

No outputs.
<!-- END_TF_DOCS -->