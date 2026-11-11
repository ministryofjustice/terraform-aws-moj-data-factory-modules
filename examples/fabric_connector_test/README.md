# Fabric Connector Example

This example composes the two reusable modules in this repository:

- `modules/fabric-oidc-provider`
- `modules/fabric-iam-role`

It creates:

- an IAM OIDC provider for Microsoft Entra
- an IAM role trusted via web identity federation
- an inline read-only S3 policy on that role

## What You Need

- AWS credentials with permissions to create IAM resources
- a Microsoft Entra tenant ID
- a Microsoft Entra service principal object ID
- an existing S3 bucket ARN to grant read access

## Finding Your Input Values

### `tenant_id` — Microsoft Entra Tenant ID

1. Go to the [Azure Portal](https://portal.azure.com)
2. Search for **Microsoft Entra ID** (formerly Azure Active Directory)
3. On the Overview page, copy the **Tenant ID** field

### `object_id` — Service Principal Object ID

This is the object ID of the specific Entra service principal (app registration) that will connect from Fabric/Power BI to S3.

1. In **Microsoft Entra ID**, open **App registrations** and find your app
2. On the app Overview page, copy the **Object ID** field (not the Application/Client ID)

Alternatively, if using a managed identity:

1. In **Microsoft Entra ID**, open **Enterprise applications**
2. Find your application and copy the **Object ID** from its Overview page

### `bucket_arn` — S3 Bucket ARN

The ARN of an **existing** S3 bucket. Format: `arn:aws:s3:::your-bucket-name`

## Quick Start

1. Copy the example input file:

```bash
cp terraform.tfvars.example terraform.tfvars
```

2. Edit `terraform.tfvars` with your values (see above for where to find them).

3. Run Terraform:

```bash
terraform init
terraform plan
terraform apply
```

4. Cleanup when finished:

```bash
terraform destroy
```

## Notes

- By default, this example uses the Power BI Amazon S3 connector audience:
  - `https://analysis.windows.net/powerbi/connector/AmazonS3`
- The role trust policy condition key prefix is derived from the OIDC provider module output to avoid mismatch errors.
