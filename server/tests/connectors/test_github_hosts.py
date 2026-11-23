"""GitHub host resolution: github.com, GitHub Enterprise Cloud and Server."""

import pytest

from connectors.github_connector import hosts

SLUG = "NEXT_PUBLIC_GITHUB_APP_SLUG"
API = "GITHUB_API_URL"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(SLUG, raising=False)
    monkeypatch.delenv(API, raising=False)
    hosts._warned.clear()


def test_bare_slug_is_github_com(monkeypatch):
    monkeypatch.setenv(SLUG, "aurora")
    assert hosts.github_web_url() == "https://github.com"
    assert hosts.github_api_url() == "https://api.github.com"
    assert hosts.github_app_install_url("s1") == (
        "https://github.com/apps/aurora/installations/new?state=s1"
    )
    assert hosts.github_mcp_host() == ""


def test_unset_slug_has_no_install_url_but_defaults_hosts():
    assert hosts.github_app_page_url() is None
    assert hosts.github_app_install_url("s1") is None
    assert hosts.github_api_url() == "https://api.github.com"


def test_ghe_com_app_url(monkeypatch):
    monkeypatch.setenv(SLUG, "https://acme.ghe.com/apps/acme/aurora/")
    assert hosts.github_web_url() == "https://acme.ghe.com"
    assert hosts.github_api_url() == "https://api.acme.ghe.com"
    assert hosts.github_app_install_url("s1") == (
        "https://acme.ghe.com/apps/acme/aurora/installations/new?state=s1"
    )
    assert hosts.github_mcp_host() == "https://acme.ghe.com"


def test_enterprise_server_app_url(monkeypatch):
    monkeypatch.setenv(SLUG, "https://git.example.com/github-apps/aurora")
    assert hosts.github_web_url() == "https://git.example.com"
    assert hosts.github_api_url() == "https://git.example.com/api/v3"
    assert hosts.github_mcp_host() == "https://git.example.com"


def test_github_com_app_url(monkeypatch):
    monkeypatch.setenv(SLUG, "https://github.com/apps/aurora")
    assert hosts.github_web_url() == "https://github.com"
    assert hosts.github_api_url() == "https://api.github.com"
    assert hosts.github_mcp_host() == ""
    assert hosts.github_app_install_url("s") == (
        "https://github.com/apps/aurora/installations/new?state=s"
    )


def test_api_override_wins(monkeypatch):
    monkeypatch.setenv(SLUG, "https://acme.ghe.com/apps/acme/aurora")
    monkeypatch.setenv(API, "https://proxy.internal/github-api/")
    assert hosts.github_api_url() == "https://proxy.internal/github-api"
    assert hosts.github_web_url() == "https://acme.ghe.com"


def test_invalid_values_fall_back_to_github_com(monkeypatch):
    monkeypatch.setenv(SLUG, "ftp://acme.ghe.com/apps/aurora")
    monkeypatch.setenv(API, "not a url")
    assert hosts.github_web_url() == "https://github.com"
    assert hosts.github_api_url() == "https://api.github.com"
    assert hosts.github_app_install_url("s") is None


def test_installation_links(monkeypatch):
    monkeypatch.setenv(SLUG, "https://acme.ghe.com/apps/acme/aurora")
    assert hosts.github_installation_settings_url(7, "acme", "Organization") == (
        "https://acme.ghe.com/organizations/acme/settings/installations/7"
    )
    assert hosts.github_installation_settings_url(7, "jo", "User") == (
        "https://acme.ghe.com/settings/installations/7"
    )
    # Enterprise avatars need a signed-in session, so none is offered.
    assert hosts.github_avatar_url("acme") is None
    assert hosts.github_avatar_url(None) is None


def test_github_com_avatar(monkeypatch):
    monkeypatch.setenv(SLUG, "aurora")
    assert hosts.github_avatar_url("acme") == "https://github.com/acme.png?size=40"


@pytest.mark.parametrize(
    "setting",
    [
        "http://acme.ghe.com/apps/acme/aurora",
        "https://user@acme.ghe.com/apps/acme/aurora",
        "https://[acme.ghe.com/apps/acme/aurora",
    ],
)
def test_unsafe_app_urls_are_refused(monkeypatch, setting):
    monkeypatch.setenv(SLUG, setting)
    assert hosts.github_web_url() == "https://github.com"
    assert hosts.github_api_url() == "https://api.github.com"
    assert hosts.github_app_install_url("s") is None


@pytest.mark.parametrize(
    "override",
    ["http://proxy.internal/gh", "https://u:p@proxy.internal/gh", "https://[proxy"],
)
def test_unsafe_api_override_is_ignored(monkeypatch, override):
    monkeypatch.setenv(SLUG, "https://acme.ghe.com/apps/acme/aurora")
    monkeypatch.setenv(API, override)
    assert hosts.github_api_url() == "https://api.acme.ghe.com"


def test_port_is_kept(monkeypatch):
    monkeypatch.setenv(SLUG, "https://git.example.com:8443/github-apps/aurora")
    assert hosts.github_api_url() == "https://git.example.com:8443/api/v3"
    assert hosts.github_app_install_url("s") == (
        "https://git.example.com:8443/github-apps/aurora/installations/new?state=s"
    )


def test_www_github_com_is_github_com(monkeypatch):
    monkeypatch.setenv(SLUG, "https://www.github.com/apps/aurora")
    assert hosts.github_web_url() == "https://github.com"
    assert hosts.github_mcp_host() == ""
    assert hosts.github_app_page_url() == "https://github.com/apps/aurora"


def test_app_url_without_app_path_has_no_install_link(monkeypatch):
    monkeypatch.setenv(SLUG, "https://acme.ghe.com")
    assert hosts.github_app_install_url("s") is None
    # The host is still known, so API calls keep working.
    assert hosts.github_api_url() == "https://api.acme.ghe.com"


def test_pasted_install_link_is_trimmed(monkeypatch):
    monkeypatch.setenv(SLUG, "https://acme.ghe.com/apps/acme/aurora/installations/new")
    assert hosts.github_app_page_url() == "https://acme.ghe.com/apps/acme/aurora"


def test_oauth_token_only_used_on_its_issuing_host(monkeypatch):
    legacy = {"access_token": "t"}
    from_com = {"access_token": "t", "api_url": "https://api.github.com"}
    from_ghe = {"access_token": "t", "api_url": "https://api.acme.ghe.com"}
    assert hosts.github_oauth_token(legacy) == "t"
    assert hosts.github_oauth_token(from_com) == "t"
    assert hosts.github_oauth_token(from_ghe) is None
    assert hosts.github_oauth_token(None) is None

    monkeypatch.setenv(SLUG, "https://acme.ghe.com/apps/acme/aurora")
    assert hosts.github_oauth_token(legacy) is None
    assert hosts.github_oauth_token(from_com) is None
    assert hosts.github_oauth_token(from_ghe) == "t"


def test_ipv6_override_keeps_brackets(monkeypatch):
    monkeypatch.setenv(API, "https://[fd00::1]:8443/api/v3")
    assert hosts.github_api_url() == "https://[fd00::1]:8443/api/v3"


def test_ipv6_zone_id_case_is_kept(monkeypatch):
    monkeypatch.setenv(API, "https://[fe80::1%25ETH0]:8443/api/v3")
    assert hosts.github_api_url() == "https://[fe80::1%25ETH0]:8443/api/v3"


def test_is_on_current_host(monkeypatch):
    assert hosts.is_on_current_host("https://github.com/o/r/pull/1")
    assert hosts.is_on_current_host("https://www.github.com/o/r/pull/1")
    assert not hosts.is_on_current_host("https://acme.ghe.com/o/r/pull/1")
    assert not hosts.is_on_current_host(None)
    monkeypatch.setenv(SLUG, "https://acme.ghe.com/apps/acme/aurora")
    assert hosts.is_on_current_host("https://acme.ghe.com/o/r/pull/1")
    assert not hosts.is_on_current_host("https://github.com/o/r/pull/1")
    assert hosts.github_identity_host() == "https://acme.ghe.com"


def test_proxy_in_front_of_github_com_keeps_stored_tokens(monkeypatch):
    """GITHUB_API_URL only reroutes traffic; github.com still issued the token."""
    monkeypatch.setenv(API, "https://proxy.internal/github-api")
    legacy = {"access_token": "t"}
    from_com = {"access_token": "t", "api_url": "https://api.github.com"}
    via_proxy = {"access_token": "t", "api_url": "https://proxy.internal/github-api"}
    assert hosts.github_oauth_token(legacy) == "t"
    assert hosts.github_oauth_token(from_com) == "t"
    assert hosts.github_oauth_token(via_proxy) == "t"
    assert hosts.github_oauth_token({"access_token": "t", "api_url": "https://api.acme.ghe.com"}) is None


@pytest.mark.parametrize("setting", ["acme.ghe.com/apps/acme/aurora", "aurora.app", "a/b"])
def test_url_without_scheme_is_not_taken_as_a_slug(monkeypatch, setting):
    monkeypatch.setenv(SLUG, setting)
    assert hosts.github_app_page_url() is None
    assert hosts.github_app_install_url("s") is None


def test_bad_settings_warn_once(monkeypatch, caplog):
    monkeypatch.setenv(SLUG, "acme.ghe.com/apps/acme/aurora")
    monkeypatch.setenv(API, "http://insecure")
    with caplog.at_level("WARNING", logger=hosts.__name__):
        for _ in range(5):
            hosts.github_app_page_url()
            hosts.github_api_url()
            hosts.github_oauth_token({"access_token": "t", "api_url": "https://api.other.ghe.com"})
    assert len(caplog.records) == 3
