"""Curated subset of Gitleaks rule IDs enabled for Aurora output redaction.

Aurora's tool outputs carry secrets from cloud providers, Kubernetes, SCM,
observability platforms, LLM vendors, and a small number of SaaS tools that
appear in runbooks. Rules outside that surface (crypto exchanges, regional
payment processors, retail/e-commerce, etc.) are excluded to reduce false
positives on customer data and keep the scan loop tight.

Bump procedure:
  1. Refetch ``rules/gitleaks-v<new>.toml`` at the pinned tag.
  2. Review the rule diff; add new IDs here only if they're in-scope.
  3. Run ``python scripts/gen_secret_patterns.py`` and commit the generated
     module alongside the new TOML.
"""

from __future__ import annotations

ALLOWED_RULE_IDS: frozenset[str] = frozenset({
    # Cloud providers
    "aws-access-token",
    "gcp-api-key",
    "azure-ad-client-secret",
    "alibaba-access-key-id",
    "alibaba-secret-key",
    "digitalocean-access-token",
    "digitalocean-pat",
    "digitalocean-refresh-token",
    "heroku-api-key",
    "heroku-api-key-v2",
    "scalingo-api-token",
    "flyio-access-token",
    "cloudflare-api-key",
    "cloudflare-global-api-key",
    "cloudflare-origin-ca-key",

    # HashiCorp / IaC
    "hashicorp-tf-api-token",
    "hashicorp-tf-password",
    "vault-batch-token",
    "vault-service-token",
    "pulumi-api-token",
    "infracost-api-token",

    # Kubernetes / container
    "kubernetes-secret-yaml",
    "openshift-user-token",

    # SCM
    "github-app-token",
    "github-fine-grained-pat",
    "github-oauth",
    "github-pat",
    "github-refresh-token",
    "gitlab-cicd-job-token",
    "gitlab-deploy-token",
    "gitlab-kubernetes-agent-token",
    "gitlab-oauth-app-secret",
    "gitlab-pat",
    "gitlab-pat-routable",
    "gitlab-ptt",
    "gitlab-rrt",
    "gitlab-runner-authentication-token",
    "gitlab-runner-authentication-token-routable",
    "gitlab-scim-token",
    "bitbucket-client-id",
    "bitbucket-client-secret",

    # Collaboration / issue tracking commonly cited in runbooks
    "atlassian-api-token",
    "linear-api-key",
    "notion-api-token",
    "slack-app-token",
    "slack-bot-token",
    "slack-config-access-token",
    "slack-config-refresh-token",
    "slack-legacy-bot-token",
    "slack-legacy-token",
    "slack-legacy-workspace-token",
    "slack-user-token",
    "slack-webhook-url",
    "discord-api-token",
    "microsoft-teams-webhook",

    # Observability / APM / logging
    "datadog-access-token",
    "grafana-api-key",
    "grafana-cloud-api-token",
    "grafana-service-account-token",
    "new-relic-browser-api-token",
    "new-relic-insert-key",
    "new-relic-user-api-id",
    "new-relic-user-api-key",
    "sentry-access-token",
    "sentry-org-token",
    "sentry-user-token",
    "sumologic-access-id",
    "sumologic-access-token",
    "dynatrace-api-token",

    # LLM / AI providers
    "openai-api-key",
    "anthropic-admin-api-key",
    "anthropic-api-key",
    "cohere-api-token",
    "huggingface-access-token",
    "huggingface-organization-api-token",
    "perplexity-api-key",
    "privateai-api-token",

    # Package registries / build
    "npm-access-token",
    "pypi-upload-token",
    "rubygems-api-token",
    "artifactory-api-key",
    "artifactory-reference-token",
    "jfrog-api-key",
    "jfrog-identity-token",
    "nuget-config-password",

    # Security / secrets tooling
    "1password-secret-key",
    "1password-service-account-token",
    "doppler-api-token",
    "snyk-api-token",
    "sonar-api-token",
    "age-secret-key",

    # Data / streaming
    "confluent-access-token",
    "confluent-secret-key",
    "databricks-api-token",
    "clickhouse-cloud-api-secret-key",
    "planetscale-api-token",
    "planetscale-oauth-token",
    "planetscale-password",

    # Generic / cross-cutting
    "jwt",
    "jwt-base64",
    "private-key",
    "curl-auth-header",
    "curl-auth-user",
    "generic-api-key",
})
