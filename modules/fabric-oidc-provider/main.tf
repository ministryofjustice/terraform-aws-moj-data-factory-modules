locals {
  issuer_url = "https://sts.windows.net/${var.tenant_id}/"

  # AWS expects the SHA1 thumbprint from the top certificate authority in the issuer chain.
  derived_thumbprint = data.tls_certificate.issuer.certificates[length(data.tls_certificate.issuer.certificates) - 1].sha1_fingerprint
}

data "tls_certificate" "issuer" {
  url = local.issuer_url
}

resource "aws_iam_openid_connect_provider" "this" {
  url = local.issuer_url

  client_id_list = [
    var.client_id,
  ]

  thumbprint_list = coalescelist(var.thumbprint_list, [local.derived_thumbprint])

  tags = {
    Name = var.oidc_provider_name
  }
}
