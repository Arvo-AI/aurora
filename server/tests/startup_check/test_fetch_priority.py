"""Tests for the two honesty fixes:

1. NO_MANIFESTS verdict — 0 parsed manifests must read "nothing to
   check", never a green SAFE.
2. Fetch priority — when a repo has more YAML than the budget, CI/docs
   YAML is excluded and manifest-likely directories (k8s/, deploy/, ...)
   win the 60-file budget regardless of alphabetical order.
"""

from services.startup_check.scanner import (
    _MAX_FILES,
    _manifest_priority,
    is_manifest_path,
    run_scan,
)
from services.startup_check.context import UserContext


# ---------------------------------------------------------------------------
# Fix 1: honest verdict
# ---------------------------------------------------------------------------


def test_zero_manifests_is_not_safe():
    """The regression from the screenshot: 60 YAML files, 0 manifests ->
    must say 'nothing to check', NOT 'engine light off'."""
    files = {
        ".github/workflows/ci.yml": "name: CI\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n",
        ".github/workflows/lint.yml": "name: Lint\non: [pull_request]\n",
        "mkdocs.yml": "site_name: Docs\ntheme: material\n",
    }
    result = run_scan(files, UserContext())
    assert result["verdict"] == "NO_MANIFESTS"
    assert "No Kubernetes manifests found" in result["summary"]
    assert result["scanned_manifests"] == 0
    # The word "SAFE" must not appear anywhere in the verdict path.
    assert result["verdict"] != "SAFE"


def test_empty_repo_is_also_no_manifests():
    result = run_scan({}, UserContext())
    assert result["verdict"] == "NO_MANIFESTS"


def test_clean_manifests_is_still_safe():
    """Guard the other direction: a real clean repo must stay SAFE."""
    files = {
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: api:1.4.2
          ports:
            - containerPort: 8080
""",
    }
    result = run_scan(files, UserContext())
    assert result["verdict"] == "SAFE"
    assert "engine light stays off" in result["summary"]


def test_risky_manifests_still_risky():
    files = {
        "k8s/svc.yaml": """
apiVersion: v1
kind: Service
metadata:
  name: api
spec:
  selector:
    app: nonexistent
  ports:
    - port: 80
""",
    }
    result = run_scan(files, UserContext())
    assert result["verdict"] == "RISKY"


# ---------------------------------------------------------------------------
# Fix 2: fetch priority
# ---------------------------------------------------------------------------


def test_priority_k8s_dirs_win_over_workflows():
    """k8s/deploy.yaml must outrank .github/workflows/ci.yml even though
    '.github' sorts first alphabetically."""
    assert _manifest_priority("k8s/deploy.yaml") == 0
    assert _manifest_priority(".github/workflows/ci.yml") == 2
    assert _manifest_priority("k8s/deploy.yaml") < _manifest_priority(
        ".github/workflows/ci.yml"
    )


def test_priority_tiers():
    # Tier 0: manifest-likely dirs (top-level and nested)
    assert _manifest_priority("k8s/deploy.yaml") == 0
    assert _manifest_priority("manifests/prod/svc.yaml") == 0
    assert _manifest_priority("infra/deploy/api.yaml") == 0
    assert _manifest_priority("helm/chart/values-note.yaml") == 0
    # Tier 1: unknown YAML — fetched when budget allows (docker-compose is
    # fetched deliberately: the agentic Layer 2 reads it as raw deploy config).
    assert _manifest_priority("config/app-settings.yaml") == 1
    assert _manifest_priority("root-manifest.yaml") == 1
    assert _manifest_priority("docker-compose.yml") == 1
    # Tier 2: never-manifest — excluded entirely
    assert _manifest_priority(".github/workflows/deploy.yml") == 2
    assert _manifest_priority("docs/example.yaml") == 2
    assert _manifest_priority("mkdocs.yml") == 2
    assert _manifest_priority("some/dir/mkdocs.yml") == 2


def test_priority_sort_puts_manifests_first():
    """A tree list where CI files alphabetically precede k8s/ must, after
    sorting, fetch the k8s files within the budget."""
    entries = [
        {"path": ".github/workflows/a.yml", "size": 100},
        {"path": ".github/workflows/b.yml", "size": 100},
        {"path": "docs/config.yml", "size": 100},
        {"path": "k8s/deploy.yaml", "size": 100},
        {"path": "k8s/svc.yaml", "size": 100},
        {"path": "zzz-unknown.yaml", "size": 100},
    ]
    yaml_entries = [
        e for e in entries
        if is_manifest_path(e["path"]) and _manifest_priority(e["path"]) < 2
    ]
    yaml_entries.sort(key=lambda e: _manifest_priority(e["path"]))
    paths = [e["path"] for e in yaml_entries[:_MAX_FILES]]
    # k8s files first, unknown after, excluded never
    assert paths.index("k8s/deploy.yaml") < paths.index("zzz-unknown.yaml")
    assert ".github/workflows/a.yml" not in paths
    assert "docs/config.yml" not in paths
    assert paths[:2] == ["k8s/deploy.yaml", "k8s/svc.yaml"]
