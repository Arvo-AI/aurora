"""Tests for deployment configuration files changed in PR.

Validates the ingress controller-agnostic changes introduced in:
- deploy/helm/aurora/values.yaml
- website/docs/deployment/kubernetes.md
"""

import pathlib

import pytest
import yaml

# Paths relative to this test file
_REPO_ROOT = pathlib.Path(__file__).parent.parent.parent
_VALUES_YAML = _REPO_ROOT / "deploy" / "helm" / "aurora" / "values.yaml"
_K8S_DOC = _REPO_ROOT / "website" / "docs" / "deployment" / "kubernetes.md"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def values() -> dict:
    """Parsed values.yaml as a Python dict."""
    with _VALUES_YAML.open() as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def values_raw() -> str:
    """Raw text content of values.yaml (for comment/annotation checks)."""
    return _VALUES_YAML.read_text()


@pytest.fixture(scope="module")
def k8s_doc() -> str:
    """Raw text content of kubernetes.md."""
    return _K8S_DOC.read_text()


# ---------------------------------------------------------------------------
# values.yaml — YAML validity
# ---------------------------------------------------------------------------


def test_values_yaml_parses_without_error():
    """values.yaml must be valid YAML."""
    with _VALUES_YAML.open() as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# values.yaml — ingress structural defaults
# ---------------------------------------------------------------------------


def test_ingress_enabled_by_default(values):
    assert values["ingress"]["enabled"] is True


def test_ingress_classname_default_is_nginx(values):
    assert values["ingress"]["className"] == "nginx"


def test_ingress_internal_false_by_default(values):
    assert values["ingress"]["internal"] is False


def test_ingress_annotations_empty_by_default(values):
    assert values["ingress"]["annotations"] == {}


# ---------------------------------------------------------------------------
# values.yaml — nginx sub-section settings
# ---------------------------------------------------------------------------


def test_nginx_section_enabled_by_default(values):
    assert values["ingress"]["nginx"]["enabled"] is True


def test_nginx_proxy_read_timeout_is_3600(values):
    assert values["ingress"]["nginx"]["proxyReadTimeout"] == "3600"


def test_nginx_proxy_send_timeout_is_3600(values):
    assert values["ingress"]["nginx"]["proxySendTimeout"] == "3600"


def test_nginx_proxy_body_size_is_50m(values):
    assert values["ingress"]["nginx"]["proxyBodySize"] == "50m"


def test_nginx_proxy_http_version_is_1_1(values):
    assert values["ingress"]["nginx"]["proxyHttpVersion"] == "1.1"


def test_nginx_session_affinity_enabled(values):
    assert values["ingress"]["nginx"]["sessionAffinity"]["enabled"] is True


def test_nginx_session_affinity_cookie_name(values):
    assert values["ingress"]["nginx"]["sessionAffinity"]["cookieName"] == "aurora-ws-affinity"


# ---------------------------------------------------------------------------
# values.yaml — comment content reflecting controller-agnostic changes
# ---------------------------------------------------------------------------


def test_classname_comment_mentions_haproxy(values_raw):
    """className comment must include haproxy as an example controller."""
    # The changed line: className: "nginx"   # Your ingress controller class (e.g., nginx, traefik, alb, haproxy)
    classname_line = next(
        line for line in values_raw.splitlines() if "className:" in line and "nginx" in line and "#" in line
    )
    assert "haproxy" in classname_line


def test_classname_comment_includes_traefik_and_alb(values_raw):
    classname_line = next(
        line for line in values_raw.splitlines() if "className:" in line and "nginx" in line and "#" in line
    )
    assert "traefik" in classname_line
    assert "alb" in classname_line


def test_warning_is_controller_agnostic(values_raw):
    """WARNING block must state it applies regardless of controller choice."""
    assert "Regardless of which controller you use" in values_raw


def test_warning_specifies_3600s_timeout(values_raw):
    """WARNING block must mention the required 3600s timeout setting."""
    assert "3600s" in values_raw


def test_warning_specifies_http_version(values_raw):
    """WARNING block must mention the required HTTP version setting."""
    assert "HTTP version" in values_raw


def test_warning_specifies_50m_body_size(values_raw):
    """WARNING block must mention the required 50m body size setting."""
    # Appears both in comment block and nginx.proxyBodySize value
    assert values_raw.count("50m") >= 1


def test_nginx_auto_apply_note_in_values(values_raw):
    """values.yaml must note that nginx settings are auto-applied as annotations."""
    assert 'When className is "nginx"' in values_raw
    assert "auto-applied as annotations" in values_raw


def test_warning_mentions_websocket_disconnects(values_raw):
    """WARNING block must warn about WebSocket disconnects consequence."""
    assert "WebSocket disconnects" in values_raw


def test_warning_mentions_413_errors(values_raw):
    """WARNING block must warn about 413 errors on file uploads."""
    assert "413" in values_raw


# ---------------------------------------------------------------------------
# kubernetes.md — Ingress Controller section exists
# ---------------------------------------------------------------------------


def test_k8s_doc_has_ingress_controller_heading(k8s_doc):
    assert "## Ingress Controller" in k8s_doc


def test_ingress_controller_section_says_controller_agnostic(k8s_doc):
    assert "controller-agnostic" in k8s_doc


def test_ingress_controller_section_references_ingressclassname(k8s_doc):
    assert "ingressClassName" in k8s_doc


def test_ingress_controller_section_mentions_ingress_classname_field(k8s_doc):
    assert "ingress.className" in k8s_doc


# ---------------------------------------------------------------------------
# kubernetes.md — Controller options table
# ---------------------------------------------------------------------------


def test_controller_table_has_nginx_row(k8s_doc):
    assert "NGINX Ingress" in k8s_doc
    assert "| `nginx`" in k8s_doc


def test_controller_table_has_traefik_row(k8s_doc):
    assert "| Traefik |" in k8s_doc
    assert "| `traefik`" in k8s_doc


def test_controller_table_has_aws_alb_row(k8s_doc):
    assert "| AWS ALB |" in k8s_doc
    assert "| `alb`" in k8s_doc


def test_controller_table_has_haproxy_row(k8s_doc):
    assert "| HAProxy |" in k8s_doc
    assert "| `haproxy`" in k8s_doc


def test_controller_table_has_four_controllers(k8s_doc):
    """Exactly four controller rows should appear in the controller options table."""
    # Scope to just the controller table: between "## Ingress Controller" and
    # the "### Required controller settings" subsection that follows it.
    section_start = k8s_doc.index("## Ingress Controller")
    subsection_start = k8s_doc.index("### Required controller settings", section_start)
    table_block = k8s_doc[section_start:subsection_start]
    # Count data rows (lines starting with '|' that are not the header row or separator)
    # The header row contains the literal column name "| Controller |" and "`className`"
    data_rows = [
        line for line in table_block.splitlines()
        if line.startswith("|")
        and not line.startswith("|---")
        and "| Controller |" not in line
        and "`className`" not in line
    ]
    assert len(data_rows) == 4


# ---------------------------------------------------------------------------
# kubernetes.md — Required controller settings table
# ---------------------------------------------------------------------------


def test_required_settings_subsection_exists(k8s_doc):
    assert "### Required controller settings" in k8s_doc


def test_required_settings_table_has_timeout_row(k8s_doc):
    assert "3600s" in k8s_doc


def test_required_settings_table_has_http_version_row(k8s_doc):
    # HTTP version 1.1 required for WebSocket
    assert "1.1" in k8s_doc


def test_required_settings_table_has_50m_upload_row(k8s_doc):
    assert "50m" in k8s_doc


def test_required_settings_mentions_rca_analysis(k8s_doc):
    """Timeout requirement must explain reason (RCA analysis duration)."""
    assert "RCA" in k8s_doc


def test_nginx_auto_applied_note_in_doc(k8s_doc):
    """Doc must state that nginx settings are auto-applied by the Helm chart."""
    assert "auto-applied as annotations" in k8s_doc


# ---------------------------------------------------------------------------
# kubernetes.md — Updated step/comment text
# ---------------------------------------------------------------------------


def test_step_1_references_ingress_controller_anchor(k8s_doc):
    """Step 1 must link to the Ingress Controller section."""
    assert "#ingress-controller" in k8s_doc


def test_step_2_comment_says_any_controller(k8s_doc):
    """Step 2 install comment must clarify any controller can be used."""
    assert "any controller that supports the Kubernetes Ingress API" in k8s_doc


def test_elb_comment_says_adjust_for_controller(k8s_doc):
    """ELB hostname resolution comment must note adjusting for non-nginx controllers."""
    assert "adjust service name/namespace for your controller" in k8s_doc


def test_certmanager_class_comment_says_change_to_match(k8s_doc):
    """cert-manager ingress class comment must instruct users to change the value."""
    assert "Change to match your ingress controller" in k8s_doc


# ---------------------------------------------------------------------------
# Regression: values.yaml ingress section is a complete, loadable sub-tree
# ---------------------------------------------------------------------------


def test_ingress_section_has_all_expected_top_level_keys(values):
    ingress = values["ingress"]
    required_keys = {"enabled", "className", "internal", "annotations", "nginx", "tls", "hosts"}
    missing = required_keys - ingress.keys()
    assert not missing, f"Missing keys in ingress section: {missing}"


def test_nginx_section_has_all_required_timeout_keys(values):
    nginx = values["ingress"]["nginx"]
    required_keys = {"enabled", "proxyReadTimeout", "proxySendTimeout", "proxyBodySize", "proxyHttpVersion"}
    missing = required_keys - nginx.keys()
    assert not missing, f"Missing keys in ingress.nginx section: {missing}"


def test_ingress_tls_disabled_by_default(values):
    """TLS must be off by default to avoid breaking installs without cert setup."""
    assert values["ingress"]["tls"]["enabled"] is False