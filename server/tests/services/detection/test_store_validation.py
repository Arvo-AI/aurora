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
