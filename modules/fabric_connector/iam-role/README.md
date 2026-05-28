# IAM Role Module

This module creates an AWS IAM role that can be assumed by Microsoft Entra identities via OpenID Connect web identity federation. The role is preconfigured with S3 bucket access permissions, enabling secure access from Power BI connectors.

## Overview

The IAM role establishes a trust relationship with a specific Entra service principal, allowing it to assume the role and access S3 resources without long-lived credentials. The role enforces strict conditions to ensure only the intended Entra service principal can assume it.

## Usage

```hcl
module "iam_role" {
  source = "./modules/iam-role"

  tenant_id         = "your-entra-tenant-id"
  object_id         = "entra-service-principal-object-id"
  oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/sts.windows.net/..."
  bucket_arn        = "arn:aws:s3:::my-bucket"
  role_name         = "cfe-fabric-s3-access"
  role_policy_name  = "cfe-fabric-s3-policy"
}
```

## Inputs

| Name | Description | Type | Required |
|------|-------------|------|----------|
| `tenant_id` | Microsoft Entra tenant ID used in IAM trust policy conditions | `string` | yes |
| `object_id` | Microsoft Entra object ID allowed to assume the IAM role | `string` | yes |
| `oidc_provider_arn` | ARN of the IAM OIDC provider trusted by this role | `string` | yes |
| `bucket_arn` | ARN of the S3 bucket this role can read | `string` | yes |
| `role_name` | IAM role name for CFE Fabric web identity access | `string` | yes |
| `role_policy_name` | Inline IAM policy name attached to the CFE Fabric role | `string` | yes |

## Outputs

| Name | Description |
|------|-------------|
| `arn` | ARN of the IAM role |

## Complete Example

```hcl
module "oidc_provider" {
  source = "./modules/oidc-provider"
  
  tenant_id          = var.tenant_id
  oidc_provider_name = "entra-powerbi"
}

module "iam_role" {
  source = "./modules/iam-role"
  
  tenant_id         = var.tenant_id
  object_id         = var.object_id
  oidc_provider_arn = module.oidc_provider.arn
  bucket_arn        = aws_s3_bucket.data_bucket.arn
  role_name         = "cfe-fabric-s3-access"
  role_policy_name  = "cfe-fabric-s3-read-policy"
}

output "iam_role_arn" {
  value = module.iam_role.arn
}
```

## Permissions Granted

The role grants the following S3 permissions:

- `s3:ListBucket` - List objects in the bucket
- `s3:GetObject` - Read/download objects from the bucket

These permissions allow the Entra service principal to list and retrieve objects from the specified S3 bucket without write access.

## Security Features

- **Web Identity Federation**: Uses OIDC instead of long-lived access keys
- **Condition-Based Trust**: Only allows assumption from a specific Entra tenant and service principal
- **Tenant Verification**: IAM conditions verify both the tenant ID and object ID match
- **Minimal Permissions**: Grants only the S3 permissions needed for read access

## How It Works

1. The OIDC provider (created by the [oidc-provider](../oidc-provider/README.md) module) is configured to trust Microsoft Entra
2. The IAM role is configured with an assume-role policy that trusts the OIDC provider
3. The assume-role policy includes conditions that restrict assumption to a specific Entra tenant and service principal
4. When the service principal requests access, it exchanges an Entra token for temporary AWS credentials
5. The credentials grant access to the specified S3 bucket

## Prerequisites

- OIDC provider deployed (use the [oidc-provider](../oidc-provider/README.md) module)
- Valid Microsoft Entra tenant ID
- Entra service principal object ID
- Target S3 bucket ARN

## Related Modules

- [oidc-provider](../oidc-provider/README.md) - Creates the OIDC provider this role trusts
