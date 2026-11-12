/**
  * # Lambda Function
  * This module build and deploys a Lambda function (Container image) to AWS. It also runs a vulnerability scan before pushing the image to ECR. The vulnerability scan threshold can be configured via the `vulnerability_scanner_threshold` variable.
  *
  * ## Usage
  * ```hcl
  * module "lambda" {
  *   source = "github.com/ministryofjustice/terraform-aws-moj-data-factory-modules//modules/lambda-function?ref=<git-sha>"
  *
  *   name = "example-lambda"
  *   lambda_path = "${path.module}/example-lambda"
  *   vulnerability_scanner_threshold = "medium"
  *
  * }
  * ```
*/

locals {
  lambda_files = fileset(var.lambda_path, "**")
  module_files = concat(
    tolist(fileset(path.module, "*.tf")),
    tolist(fileset(path.module, "*.sh"))
  )

  lambda_hash = sha256(join("", concat(
    [
      for file in sort(local.lambda_files) :
      filesha256("${var.lambda_path}/${file}")
    ],
    [
      for file in sort(local.module_files) :
      filesha256("${path.module}/${file}")
    ]
  )))

  image_uri = "${aws_ecr_repository.lambda.repository_url}:${local.lambda_hash}"
}

# ECR Registry
resource "aws_ecr_repository" "lambda" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "terraform_data" "docker_build" {
  triggers_replace = {
    hash = local.lambda_hash
  }

  provisioner "local-exec" {
    command = "${path.module}/docker_build.sh"

    environment = {
      REGION         = var.region
      ECR_REPOSITORY = aws_ecr_repository.lambda.name
      LAMBDA_PATH    = var.lambda_path
      HASH           = local.lambda_hash
      VULNERABILITY_SCANNER_THRESHOLD = var.vulnerability_scanner_threshold
    }
  }
}

resource "aws_iam_role" "lambda" {
  name = "${var.name}-lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_lambda_function" "lambda" {
  function_name = var.name
  role          = aws_iam_role.lambda.arn

  package_type = "Image"
  image_uri    = local.image_uri
  architectures = ["arm64"]

  timeout     = 30
  memory_size = 128

  # Ensure docker_build.sh has built + pushed the image first.
  depends_on = [
    terraform_data.docker_build,
    aws_iam_role_policy_attachment.lambda_basic,
  ]
}

output "hash" {
  value = local.lambda_hash
}
