.PHONY: import-oidc

import-oidc: ## Import a pre-existing OIDC provider (OIDC_PROVIDER_ARN=<arn> make import-oidc)
	@test -n "$(OIDC_PROVIDER_ARN)" || (echo "Usage: OIDC_PROVIDER_ARN=<arn> make import-oidc"; exit 1)
	terraform import module.oidc_provider.aws_iam_openid_connect_provider.this $(OIDC_PROVIDER_ARN)
