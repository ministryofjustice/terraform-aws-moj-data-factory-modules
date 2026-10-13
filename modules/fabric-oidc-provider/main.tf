locals {
  issuer_url = "https://sts.windows.net/${var.tenant_id}/"

  # AWS expects the SHA1 thumbprint from the top certificate authority in the issuer chain.
  # Only derived when no explicit thumbprint_list override is supplied.
  derived_thumbprint = var.thumbprint_list == null ? data.tls_certificate.issuer[0].certificates[length(data.tls_certificate.issuer[0].certificates) - 1].sha1_fingerprint : null
}

# Skipped entirely when thumbprint_list is provided, avoiding a network call to the issuer.
data "tls_certificate" "issuer" {
  count = var.thumbprint_list == null ? 1 : 0
  url   = local.issuer_url
}

resource "aws_iam_openid_connect_provider" "this" {
  url = local.issuer_url

  client_id_list = [
    var.client_id,
  ]

  thumbprint_list = var.thumbprint_list != null ? var.thumbprint_list : [local.derived_thumbprint]

  tags = merge(
    { Name = var.oidc_provider_name },
    var.additional_tags,
  )
}
