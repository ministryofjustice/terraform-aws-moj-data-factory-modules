# OIDC Provider Module

This module creates an AWS IAM OpenID Connect (OIDC) provider that establishes a trust relationship with Microsoft Entra (Azure AD). This allows Entra identities to assume IAM roles in AWS without needing long-lived credentials.

## Overview

The OIDC provider acts as a federated identity provider, enabling secure authentication between Microsoft Entra and AWS using OpenID Connect. This is specifically configured for Power BI connectors to access Amazon S3 resources.

## Usage

```hcl
module "oidc_provider" {
  source = "./modules/oidc-provider"

  tenant_id           = "your-entra-tenant-id"
  oidc_provider_name  = "entra-powerbi-provider"
}
```

## Outputs Usage

The ARN output from this module should be passed to the [iam-role](../iam-role/README.md) module as the `oidc_provider_arn` variable to establish the trust relationship.

```hcl
module "oidc_provider" {
  source = "./modules/oidc-provider"
  
  tenant_id          = var.tenant_id
  oidc_provider_name = "entra-powerbi-provider"
}

module "iam_role" {
  source = "./modules/iam-role"
  
  oidc_provider_arn = module.oidc_provider.arn
  # ... other variables
}
```

## How It Works

The OIDC provider is created with:

- **Provider URL**: `https://sts.windows.net/{tenant_id}/` - Points to Microsoft Entra's token endpoint
- **Client ID**: `https://analysis.windows.net/powerbi/connector/AmazonS3` - Identifies Power BI as the client requesting authentication

When a Power BI service principal in Entra attempts to access AWS resources, it exchanges an Entra token for temporary AWS credentials via the OIDC provider.

## Requirements

- Valid Microsoft Entra tenant ID

## Related Modules

- [iam-role](../iam-role/README.md) - Creates IAM roles that trust this OIDC provider

<!-- BEGIN_TF_DOCS -->
## Requirements

No requirements.

## Providers

| Name | Version |
| ---- | ------- |
| <a name="provider_aws"></a> [aws](#provider\_aws) | 6.47.0 |

## Modules

No modules.

## Resources

| Name | Type |
| ---- | ---- |
| [aws_iam_openid_connect_provider.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_openid_connect_provider) | resource |

## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| <a name="input_oidc_provider_name"></a> [oidc\_provider\_name](#input\_oidc\_provider\_name) | Tag name for the AWS IAM OIDC provider. | `string` | n/a | yes |
| <a name="input_tenant_id"></a> [tenant\_id](#input\_tenant\_id) | Microsoft Entra tenant ID used in OIDC provider URL. | `string` | n/a | yes |

## Outputs

| Name | Description |
| ---- | ----------- |
| <a name="output_arn"></a> [arn](#output\_arn) | ARN of the IAM OIDC provider. |
<!-- END_TF_DOCS -->