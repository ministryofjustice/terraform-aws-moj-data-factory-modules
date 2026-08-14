<!-- BEGIN_TF_DOCS -->
# Lambda Function
This module build and deploys a Lambda function (Container image) to AWS. It also runs a vulnerability scan before pushing the image to ECR. The vulnerability scan threshold can be configured via the `vulnerability_scanner_threshold` variable.

## Usage
```hcl
module "lambda" {
  source = "github.com/ministryofjustice/terraform-aws-moj-data-factory-modules//modules/lambda-function?ref=<git-sha>"

  name = "example-lambda"
  lambda_path = "${path.module}/example-lambda"
  vulnerability_scanner_threshold = "medium"

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
| <a name="provider_terraform"></a> [terraform](#provider\_terraform) | n/a |

## Modules

No modules.

## Resources

| Name | Type |
|------|------|
| [aws_ecr_repository.lambda](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/ecr_repository) | resource |
| [aws_iam_role.lambda](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role_policy_attachment.lambda_basic](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy_attachment) | resource |
| [aws_lambda_function.lambda](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lambda_function) | resource |
| [terraform_data.docker_build](https://registry.terraform.io/providers/hashicorp/terraform/latest/docs/resources/data) | resource |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_lambda_path"></a> [lambda\_path](#input\_lambda\_path) | The path to the lambda function directory | `string` | n/a | yes |
| <a name="input_name"></a> [name](#input\_name) | The name of the lambda function | `string` | n/a | yes |
| <a name="input_region"></a> [region](#input\_region) | The AWS region to deploy the lambda function | `string` | `"eu-west-2"` | no |
| <a name="input_vulnerability_scanner_threshold"></a> [vulnerability\_scanner\_threshold](#input\_vulnerability\_scanner\_threshold) | The threshold for the vulnerability scanner | `string` | `"high"` | no |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_hash"></a> [hash](#output\_hash) | n/a |
<!-- END_TF_DOCS -->