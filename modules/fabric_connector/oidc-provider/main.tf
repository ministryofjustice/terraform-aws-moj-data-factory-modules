resource "aws_iam_openid_connect_provider" "this" {
  url = "https://sts.windows.net/${var.tenant_id}/"

  client_id_list = [
    var.client_id,
  ]

  tags = {
    Name = var.oidc_provider_name
  }
}
