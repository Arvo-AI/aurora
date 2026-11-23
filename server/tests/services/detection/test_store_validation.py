"""What may be saved as a check.

A check runs unattended with the org's cloud access, so the definition is the
only place a write could get in. These cases are the ways one could.
"""

import pytest

from services.detection.store import normalize_definition, normalize_origin


def _definition(**overrides):
    raw = {
        "name": "checkout crashloops",
        "prediction": "checkout pods enter CrashLoopBackOff",
        "asset": "checkout",
        "pattern": "Pod-CrashLoop",
        "provider": "GCP",
        "command": "kubectl get pods -n checkout --no-headers | grep -c CrashLoopBackOff",
        "assertion": {"expect": "number", "op": "eq", "value": 0},
        "interval_seconds": 300,
    }
    raw.update(overrides)
    return raw


def test_a_read_only_check_is_accepted_and_keys_are_normalised():
    definition, problem = normalize_definition(_definition())
    assert problem == ""
    # Pattern and provider are grouping keys: case drift in model output must
    # not split one pattern's record in two.
    assert definition["pattern"] == "pod-crashloop"
    assert definition["provider"] == "gcp"


@pytest.mark.parametrize("command", [
    "kubectl delete pod checkout-1 -n checkout",
    "kubectl get pods -n checkout && kubectl delete pod checkout-1 -n checkout",
    "kubectl get cm script -o jsonpath='{.data.sh}' | bash",
    "kubectl get pods -n checkout > /tmp/pods",
    "kubectl get secrets -n checkout",
])
def test_a_command_that_writes_or_reads_credentials_is_refused(command):
    definition, problem = normalize_definition(_definition(command=command))
    assert definition is None
    assert "not read-only" in problem


@pytest.mark.parametrize("interval", [60, 299, True, "300", 300.0, 2 ** 31])
def test_an_interval_below_the_floor_or_not_an_integer_is_refused(interval):
    definition, problem = normalize_definition(_definition(interval_seconds=interval))
    assert definition is None
    assert "interval_seconds" in problem


def test_a_check_with_no_stated_failure_is_refused():
    definition, problem = normalize_definition(_definition(prediction="   "))
    assert definition is None
    assert "prediction" in problem


def test_origin_must_be_a_known_kind():
    assert normalize_origin({"kind": "incident", "ref": "INC 12"})[0] == {"kind": "incident", "ref": "INC 12"}
    assert normalize_origin({"kind": "because", "ref": ""})[0] is None


def test_an_onprem_check_needs_its_cluster_and_cannot_use_a_pipe():
    # That executor has no shell: a pipe would reach kubectl as arguments and
    # the check would error on every run, forever.
    onprem = dict(provider="onprem_kubectl", command="kubectl get pods -n checkout --no-headers",
                  assertion={"expect": "lines", "op": "gte", "value": 1})
    assert normalize_definition(_definition(**onprem))[0] is None
    assert normalize_definition(_definition(**onprem, target="cluster-7"))[1] == ""
    piped = dict(onprem, target="cluster-7",
                 command="kubectl get pods -n checkout --no-headers | grep -c Running")
    assert "no pipes" in normalize_definition(_definition(**piped))[1]


@pytest.mark.parametrize("field", ["name", "asset", "pattern"])
def test_an_identifier_cannot_carry_chat_mention_syntax(field):
    # These reach an incident title that the agent posting to chat reads.
    definition, problem = normalize_definition(_definition(**{field: "checkout <!channel>"}))
    assert definition is None
    assert field in problem


def test_a_run_that_names_no_claim_releases_none():
    # A task queued by an earlier version carries no claim. It must not clear
    # the claim of the run that has since replaced it.
    from services.detection.store import _release_claim

    class Cursor:
        statements = []

        def execute(self, sql, params=None):
            self.statements.append(sql)

    cursor = Cursor()
    _release_claim(cursor, "check-1", None)
    assert cursor.statements == []
    _release_claim(cursor, "check-1", "2026-11-20T10:00:00+00:00")
    assert len(cursor.statements) == 1


@pytest.mark.parametrize("asset", [
    "nginx deployment (demo namespace, kind-aurora-detect)",
    "checkout service",
    "checkout,payments",
])
def test_an_asset_must_be_the_bare_name_an_incident_would_carry(asset):
    # A check is credited only when an incident names the same asset. A
    # descriptive asset can never match one, so the check could never be promoted.
    definition, problem = normalize_definition(_definition(asset=asset))
    assert definition is None
    assert "asset" in problem


@pytest.mark.parametrize("asset", ["checkout", "orders-db", "shop/checkout", "api.v2", "queue:orders"])
def test_ordinary_service_names_are_accepted(asset):
    assert normalize_definition(_definition(asset=asset))[1] == ""


@pytest.mark.parametrize("command", [
    "kubectl get pods -n checkout -w",
    "kubectl get pods -n checkout --watch=true",
    "kubectl logs deploy/checkout -n checkout --follow",
    "kubectl logs deploy/checkout -n checkout -f",
])
def test_a_command_that_never_finishes_is_refused(command):
    # These are reads, so the read-only classifier lets them through, but each
    # run would hold a worker until the timeout.
    definition, problem = normalize_definition(_definition(command=command))
    assert definition is None
    assert "answer once" in problem


def test_only_flags_that_keep_a_command_open_are_refused():
    from services.detection.store import streaming_flag

    never_finish = (
        "kubectl get pods -w", "kubectl get pods --watch", "kubectl get pods --watch-only",
        "kubectl logs -f deploy/checkout", "kubectl logs deploy/checkout --follow=true",
    )
    answer_once = (
        "kubectl get deployments -f deployment.yaml",       # -f names a file here
        "kubectl get pods --watch=false",
        "kubectl get pods -n shop",
    )
    assert all(streaming_flag(c) for c in never_finish)
    assert not any(streaming_flag(c) for c in answer_once)
