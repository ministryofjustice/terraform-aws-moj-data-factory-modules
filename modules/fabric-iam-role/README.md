# IAM Role Module

This module creates an AWS IAM role that can be assumed by Microsoft Entra identities via OpenID Connect web identity federation. The role is preconfigured with S3 bucket access permissions, enabling secure access from Power BI connectors.

## Overview

The IAM role establishes a trust relationship with a specific Entra service principal, allowing it to assume the role and access S3 resources without long-lived credentials. The role enforces strict conditions to ensure only the intended Entra service principal can assume it.

## Usage

```hcl
module "iam_role" {
  source = "github.com/ministryofjustice/terraform-aws-moj-data-factory-modules//modules/fabric-iam-role?ref=<git-ref>"

  object_id                          = "00000000-0000-0000-0000-000000000000"
  oidc_provider_arn                  = "arn:aws:iam::123456789012:oidc-provider/sts.windows.net/00000000-0000-0000-0000-000000000000/"
  oidc_provider_condition_key_prefix = "sts.windows.net/00000000-0000-0000-0000-000000000000/:"
  bucket_arn                         = "arn:aws:s3:::my-bucket"
  role_name                          = "cfe-fabric-s3-access"
  role_policy_name                   = "cfe-fabric-s3-policy"
}
```

## Complete Example

```hcl
module "oidc_provider" {
  source = "github.com/ministryofjustice/terraform-aws-moj-data-factory-modules//modules/fabric-oidc-provider?ref=<git-ref>"

  tenant_id          = var.tenant_id
  oidc_provider_name = "entra-powerbi"
}

module "iam_role" {
  source = "github.com/ministryofjustice/terraform-aws-moj-data-factory-modules//modules/fabric-iam-role?ref=<git-ref>"

  object_id                          = var.object_id
  oidc_provider_arn                  = module.oidc_provider.arn
  oidc_provider_condition_key_prefix = module.oidc_provider.condition_key_prefix
  audience                           = module.oidc_provider.client_id
  bucket_arn                         = aws_s3_bucket.data_bucket.arn
  role_name                          = "cfe-fabric-s3-access"
  role_policy_name                   = "cfe-fabric-s3-read-policy"
}

output "iam_role_arn" {
  value = module.iam_role.arn
}
```

## Permissions Granted

The role grants the minimum S3 permissions required for Fabric/Power BI S3 shortcuts:

- `s3:ListBucket` - List objects in the bucket (bucket-level)
- `s3:GetBucketLocation` - Resolve the bucket's region (bucket-level)
- `s3:GetObject` - Read/download objects from the bucket (object-level)
- `s3:GetObjectAttributes` - Read object metadata/attributes (object-level)

These permissions allow the Entra service principal to list and retrieve objects from the specified S3 bucket without write access.

## Security Features

- **Web Identity Federation**: Uses OIDC instead of long-lived access keys
- **Condition-Based Trust**: Restricts assumption to the expected audience (`aud`) and Entra service principal subject (`sub`)
- **Tenant Scoping**: Tenant isolation is inherited from the OIDC provider issuer (the `oidc_provider_condition_key_prefix`), not a separate tenant-id condition
- **Minimal Permissions**: Grants only the S3 permissions needed for read access

## How It Works

1. The OIDC provider (created by the [fabric-oidc-provider](../fabric-oidc-provider/README.md) module) is configured to trust Microsoft Entra
2. The IAM role is configured with an assume-role policy that trusts the OIDC provider
3. The assume-role policy includes conditions that restrict assumption to the expected audience (`aud`) and service principal subject (`sub`), with tenant scoping inherited from the OIDC provider issuer
4. When the service principal requests access, it exchanges an Entra token for temporary AWS credentials
5. The credentials grant access to the specified S3 bucket

## Prerequisites

- OIDC provider deployed (use the [fabric-oidc-provider](../fabric-oidc-provider/README.md) module)
- Valid Microsoft Entra tenant ID
- Entra Enterprise Application (service principal) Object ID
- Target S3 bucket ARN

## Related Modules

- [fabric-oidc-provider](../fabric-oidc-provider/README.md) - Creates the OIDC provider this role trusts

<!-- BEGIN_TF_DOCS -->
<!-- markdownlint-disable -->
<!-- prettier-ignore-start -->
## Requirements

| Name | Version |
|------|---------|
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.6.0 |
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
| [aws_iam_role.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role_policy.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_iam_policy_document.bucket_access](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.trust](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_additional_tags"></a> [additional\_tags](#input\_additional\_tags) | Additional tags to apply to resources created by this module. | `map(string)` | `{}` | no |
| <a name="input_audience"></a> [audience](#input\_audience) | Expected OIDC audience (`aud`) claim. Defaults to the Power BI Amazon S3 connector audience. | `string` | `"https://analysis.windows.net/powerbi/connector/AmazonS3"` | no |
| <a name="input_bucket_arn"></a> [bucket\_arn](#input\_bucket\_arn) | ARN of the S3 bucket this role can read. | `string` | n/a | yes |
| <a name="input_object_id"></a> [object\_id](#input\_object\_id) | Enterprise Application (service principal) Object ID of the Microsoft Entra identity allowed to assume the IAM role. This is the Object ID found under Enterprise applications > <app> > Overview, NOT the App registration Object ID. | `string` | n/a | yes |
| <a name="input_oidc_provider_arn"></a> [oidc\_provider\_arn](#input\_oidc\_provider\_arn) | ARN of the IAM OIDC provider trusted by this role. | `string` | n/a | yes |
| <a name="input_oidc_provider_condition_key_prefix"></a> [oidc\_provider\_condition\_key\_prefix](#input\_oidc\_provider\_condition\_key\_prefix) | Condition key prefix from the fabric-oidc-provider module output (e.g. 'sts.windows.net/{tenant\_id}/:'). | `string` | n/a | yes |
| <a name="input_role_name"></a> [role\_name](#input\_role\_name) | IAM role name for Fabric web identity access. | `string` | n/a | yes |
| <a name="input_role_policy_name"></a> [role\_policy\_name](#input\_role\_policy\_name) | Inline IAM policy name attached to the IAM role. | `string` | n/a | yes |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_arn"></a> [arn](#output\_arn) | ARN of the IAM role. |
<!-- prettier-ignore-end -->
<!-- markdownlint-enable -->
<!-- END_TF_DOCS -->
