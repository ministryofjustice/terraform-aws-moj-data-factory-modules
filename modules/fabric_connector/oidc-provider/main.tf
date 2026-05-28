resource "aws_iam_openid_connect_provider" "this" {
  url = "https://sts.windows.net/${var.tenant_id}/"

  client_id_list = [
    "https://analysis.windows.net/powerbi/connector/AmazonS3",
  ]

  tags = {
    Name = var.oidc_provider_name
  }
}
