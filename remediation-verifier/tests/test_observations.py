"""Black-box tests for the snapshot reader and writer.

Every test starts from the canonical document in the specification and breaks
exactly one part of it, so a failure points at one rule and at the metadata name
that rule must report.
"""

from __future__ import annotations

import copy
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from verifier.model import ObservationError
from verifier.observations import (
    MODE_LIVE,
    MODE_SNAPSHOT,
    PROVENANCE_LIVE,
    from_snapshot,
    snapshot_document,
    to_snapshot,
)

LAB_COMMIT = "4edcd9dfbc48cbd9ca8ea031e429832cf72842d0"
CAPTURED_AT = "2026-11-15T20:00:00Z"
ROUTE = "http://frontend.shop.svc.cluster.local/api/items"
SECTIONS = {"pods", "services", "endpoints", "http", "logs"}


def canonical() -> dict[str, Any]:
    """Return the canonical snapshot document of the specification."""
    return {
        "meta": {
            "lab_commit": LAB_COMMIT,
            "variant": "baseline",
            "mode": "LIVE",
            "captured_at": CAPTURED_AT,
            "provenance": "LIVE",
        },
        "pods": [
            {
                "name": "worker-1",
                "uid": "u1",
                "phase": "Running",
                "ready": False,
                "restart_count": 4,
                "waiting_reason": "CrashLoopBackOff",
                "last_terminated_reason": "OOMKilled",
                "last_exit_code": 137,
                "containers": [
                    {
                        "name": "worker",
                        "ready": False,
                        "restart_count": 4,
                        "waiting_reason": "CrashLoopBackOff",
                        "last_terminated_reason": "OOMKilled",
                        "last_exit_code": 137,
                    }
                ],
            }
        ],
        "services": [
            {
                "name": "frontend",
                "selector": {"app": "frontend", "tier": "web"},
                "ports": [80],
                "cluster_ip": "10.96.0.10",
            }
        ],
        "endpoints": [
            {"name": "frontend-abc", "addresses": [{"address": "10.244.0.5", "ready": False}]}
        ],
        "http": [
            {
                "route": ROUTE,
                "status": 503,
                "body": "",
                "duration_seconds": 0.12,
                "error": None,
            }
        ],
        "logs": {
            "worker": ["processed batch of 2500 items"],
            "loadgen": ["GET /api/items -> 503"],
        },
        "worker": {"batch_size": 2500},
    }


def fresh() -> dict[str, Any]:
    return copy.deepcopy(canonical())


class TestReading:
    def test_a_complete_document_has_no_missing_metadata(self) -> None:
        observations = from_snapshot(fresh())
        assert observations.missing_metadata == ()
        assert observations.is_complete

    def test_require_complete_returns_the_same_object(self) -> None:
        observations = from_snapshot(fresh())
        assert observations.require_complete() is observations

    @pytest.mark.parametrize("section", sorted(SECTIONS))
    def test_each_missing_section_is_named(self, section: str) -> None:
        data = fresh()
        del data[section]
        observations = from_snapshot(data)
        assert section in observations.missing_metadata
        assert set(observations.missing_metadata) & SECTIONS == {section}

    def test_logs_without_a_worker_key_report_worker_logs(self) -> None:
        data = fresh()
        data["logs"] = {"loadgen": ["GET /api/items -> 503"]}
        assert "worker_logs" in from_snapshot(data).missing_metadata

    def test_a_pod_without_a_restart_count_reports_restart_counts(self) -> None:
        data = fresh()
        del data["pods"][0]["restart_count"]
        del data["pods"][0]["containers"][0]["restart_count"]
        assert "restart_counts" in from_snapshot(data).missing_metadata

    def test_a_document_without_the_worker_section_reports_the_batch_size(self) -> None:
        data = fresh()
        del data["worker"]
        assert "batch_size" in from_snapshot(data).missing_metadata

    @pytest.mark.parametrize("field", ["lab_commit", "variant"])
    def test_an_absent_identity_field_is_named(self, field: str) -> None:
        data = fresh()
        del data["meta"][field]
        assert field in from_snapshot(data).missing_metadata

    @pytest.mark.parametrize("field", ["lab_commit", "variant"])
    def test_an_empty_identity_field_is_named(self, field: str) -> None:
        data = fresh()
        data["meta"][field] = ""
        assert field in from_snapshot(data).missing_metadata

    @pytest.mark.parametrize("captured_at", ["", "yesterday"])
    def test_an_invalid_captured_at_is_named(self, captured_at: str) -> None:
        data = fresh()
        data["meta"]["captured_at"] = captured_at
        assert "captured_at" in from_snapshot(data).missing_metadata

    @pytest.mark.parametrize("provenance", ["SNAPSHOT", "RECORDED", ""])
    def test_a_provenance_other_than_live_is_named(self, provenance: str) -> None:
        data = fresh()
        data["meta"]["provenance"] = provenance
        assert "provenance" in from_snapshot(data).missing_metadata

    def test_loading_marks_the_mode_as_snapshot_and_keeps_provenance(self) -> None:
        observations = from_snapshot(fresh())
        assert observations.meta.mode == MODE_SNAPSHOT
        assert observations.meta.provenance == PROVENANCE_LIVE

    def test_require_complete_names_the_missing_fields(self) -> None:
        data = fresh()
        del data["pods"]
        del data["logs"]
        observations = from_snapshot(data)
        with pytest.raises(ObservationError) as excinfo:
            observations.require_complete()
        message = str(excinfo.value)
        assert "pods" in message
        assert "logs" in message

    def test_a_mapping_is_accepted(self) -> None:
        assert from_snapshot(fresh()).is_complete

    def test_a_written_path_is_accepted_as_a_path_and_as_a_string(self, tmp_path: Path) -> None:
        path = to_snapshot(from_snapshot(fresh()), tmp_path / "observation.json")
        assert from_snapshot(path).is_complete
        assert from_snapshot(str(path)).is_complete

    def test_a_pods_section_that_is_not_a_list_raises(self) -> None:
        data = fresh()
        data["pods"] = {"name": "worker-1"}
        with pytest.raises(ObservationError):
            from_snapshot(data)

    def test_reading_never_runs_a_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("from_snapshot ran a subprocess")

        monkeypatch.setattr(subprocess, "run", refuse)
        assert from_snapshot(fresh()).is_complete
        path = to_snapshot(from_snapshot(fresh()), tmp_path / "observation.json")
        assert from_snapshot(path).is_complete


class TestWriting:
    def test_the_document_has_the_seven_sections_and_no_timing(self) -> None:
        written = snapshot_document(from_snapshot(fresh()))
        assert set(written) == {
            "meta",
            "pods",
            "services",
            "endpoints",
            "http",
            "logs",
            "worker",
        }
        assert "timing" not in json.dumps(written)
        assert written["meta"]["mode"] == MODE_SNAPSHOT

    def test_the_written_file_is_deterministic_and_ends_with_a_newline(
        self, tmp_path: Path
    ) -> None:
        path = to_snapshot(from_snapshot(fresh()), tmp_path / "observation.json")
        text = path.read_text(encoding="utf-8")
        assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"
        assert "timing" not in text

    def test_a_reload_equals_the_original_except_the_mode(self, tmp_path: Path) -> None:
        observations = from_snapshot(fresh())
        path = to_snapshot(observations, tmp_path / "observation.json")
        restored = from_snapshot(path)
        assert restored.meta.mode == MODE_SNAPSHOT
        assert replace(restored, meta=replace(restored.meta, mode=MODE_LIVE)) == replace(
            observations, meta=replace(observations.meta, mode=MODE_LIVE)
        )
