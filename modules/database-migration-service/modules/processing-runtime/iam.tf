data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "receiver" {
  name               = var.function_name
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = var.tags
}

resource "aws_iam_role" "worker" {
  name               = "${var.function_name}-worker"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = var.tags
}

data "aws_iam_policy_document" "receiver_operations" {
  statement {
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
    ]
    resources = [aws_dynamodb_table.operations.arn]
  }
}

resource "aws_iam_role_policy" "receiver_operations" {
  name   = "processing-operations"
  role   = aws_iam_role.receiver.name
  policy = data.aws_iam_policy_document.receiver_operations.json
}

data "aws_iam_policy_document" "worker_operations" {
  statement {
    actions = [
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
    ]
    resources = [aws_dynamodb_table.operations.arn]
  }

  statement {
    actions = [
      "dynamodb:DescribeStream",
      "dynamodb:GetRecords",
      "dynamodb:GetShardIterator",
      "dynamodb:ListStreams",
    ]
    resources = [aws_dynamodb_table.operations.stream_arn]
  }
}

resource "aws_iam_role_policy" "worker_operations" {
  name   = "processing-operations"
  role   = aws_iam_role.worker.name
  policy = data.aws_iam_policy_document.worker_operations.json
}
