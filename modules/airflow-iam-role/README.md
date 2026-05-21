# Data Factory Airflow IAM Role

Manages an AWS IAM role in a Justice Data Factory for use in Justice Data Platform Airflow workloads, including trust policy, optional managed policies, and optional inline policies.

The generated role name is `airflow-<account>-<role_name_suffix>`, where `<account>` is `prod` for `production` and `preproduction`, `test` for `test`, and `dev` for `development`.
`<role_name_suffix>` is appended with `-pp` when `<account>` is `preproduction` to avoid name clash.
This role name can be inserted into the given Airflow workflow file in the `analytical-platform-airflow` [repository](https://github.com/ministryofjustice/analytical-platform-airflow/) under `iam: external_role:` (see documentation [here](https://user-guidance.analytical-platform.service.justice.gov.uk/services/airflow/#external-iam-roles)).

## Usage

```hcl
module "data_platform_airflow_iam_role" {
  source = "github.com/ministryofjustice/terraform-aws-moj-data-factory-modules//modules/airflow-iam-role?ref=v1.0.0"

  application_name   = local.application_name
  environment        = local.environment
  oidc_arn           = aws_iam_openid_connect_provider.analytical_platform_compute.arn
  role_name_suffix   = "data-ingestion"
  role_description   = "Example IAM role for an Airflow workflow"
  secret_code        = jsondecode(data.aws_secretsmanager_secret_version.airflow_secret.secret_string)["oidc_cluster_identifier"]

  iam_policy_documents = [
    jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Effect = "Allow"
          Action = [
            "s3:GetObject",
            "s3:PutObject"
          ]
          Resource = "arn:aws:s3:::some-example-bucket/*"
        }
      ]
    })
  ]
}
```

<!-- prettier-ignore-start -->
<!-- markdownlint-disable -->

<!-- BEGIN_TF_DOCS -->
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
| [aws_iam_policy.role_ap_airflow](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_policy) | resource |
| [aws_iam_role.role_ap_airflow](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role_policy_attachment.role_ap_airflow](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy_attachment) | resource |
| [aws_iam_policy_document.oidc_assume_role_policy](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_application_name"></a> [application\_name](#input\_application\_name) | Application name used in the MWAA service account identifier. Use `local.application_name` | `string` | n/a | yes |
| <a name="input_environment"></a> [environment](#input\_environment) | Application name used in the MWAA service account identifier (use `local.application_name` as defined in `platform.locals.tf`) | `string` | n/a | yes |
| <a name="input_iam_policy_documents"></a> [iam\_policy\_documents](#input\_iam\_policy\_documents) | List of IAM policy JSON documents to create and attach to the role | `list(string)` | n/a | yes |
| <a name="input_max_session_duration"></a> [max\_session\_duration](#input\_max\_session\_duration) | Maximum session duration in seconds | `number` | `3600` | no |
| <a name="input_oidc_arn"></a> [oidc\_arn](#input\_oidc\_arn) | ARN of the OIDC identity provider | `string` | n/a | yes |
| <a name="input_role_description"></a> [role\_description](#input\_role\_description) | Description of the IAM role | `string` | n/a | yes |
| <a name="input_role_name_suffix"></a> [role\_name\_suffix](#input\_role\_name\_suffix) | Suffix used when constructing the IAM role name | `string` | n/a | yes |
| <a name="input_secret_code"></a> [secret\_code](#input\_secret\_code) | OIDC issuer ID segment used in condition keys | `string` | n/a | yes |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_role_arn"></a> [role\_arn](#output\_role\_arn) | ARN of the IAM role |
| <a name="output_role_name"></a> [role\_name](#output\_role\_name) | Name of the IAM role |
| <a name="output_role_unique_id"></a> [role\_unique\_id](#output\_role\_unique\_id) | Stable and unique string identifying the IAM role |
<!-- END_TF_DOCS -->

<!-- markdownlint-enable -->
<!-- prettier-ignore-end -->
