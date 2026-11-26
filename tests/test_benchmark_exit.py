"""Exit behaviour of scripts/benchmark.py: failures and "not run" are never a pass."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark.py"


@pytest.fixture()
def bench():
    import importlib.util

    spec = importlib.util.spec_from_file_location("ziko_benchmark_exit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _only(bench, monkeypatch, **suites):
    monkeypatch.setattr(bench, "SUITES", suites)


def _run(bench, tmp_path, *argv):
    out = tmp_path / "r.json"
    rc = bench.main(["--quick", *argv, "--out", str(out)])
    return rc, json.loads(out.read_text())


def test_passing_suite_exits_zero(bench, monkeypatch, tmp_path):
    _only(bench, monkeypatch, good=(lambda a, w: {"x": {"ms": 1.0}}, ()))
    rc, data = _run(bench, tmp_path, "--strict")
    assert rc == 0 and data["status"] == {"good": "passed"} and data["exit_code"] == 0


def test_duplicate_reply_guard_exits_3(bench, monkeypatch, tmp_path):
    bad = {"duplicate_updates@c10": {"INVALID": "sent 2 replies, expected 1"}}
    _only(bench, monkeypatch, engine=(lambda a, w: bad, ()))
    rc, data = _run(bench, tmp_path)
    assert rc == 3 and data["status"]["engine"] == "invalid"


def test_suite_exception_exits_4_even_without_strict(bench, monkeypatch, tmp_path):
    def boom(a, w):
        raise RuntimeError("database locked")
    _only(bench, monkeypatch, engine=(boom, ()), good=(lambda a, w: {}, ()))
    rc, data = _run(bench, tmp_path)
    assert rc == 4 and data["status"]["engine"] == "failed"
    assert "engine" not in data["results"] and "database locked" in data["failed"]["engine"]


def test_import_failure_is_not_run_and_fails_under_strict(bench, monkeypatch, tmp_path):
    _only(bench, monkeypatch, engine=(lambda a, w: {}, ("module_that_does_not_exist_zz",)))
    rc, data = _run(bench, tmp_path, "--strict")
    assert rc == 5 and data["status"]["engine"] == "not_run"
    assert "engine" not in data["results"]


def test_not_run_is_never_reported_as_passed(bench, monkeypatch, tmp_path):
    _only(bench, monkeypatch, engine=(lambda a, w: {}, ("module_that_does_not_exist_zz",)),
          good=(lambda a, w: {}, ()))
    rc, data = _run(bench, tmp_path)
    assert data["status"] == {"engine": "not_run", "good": "passed"}
    assert rc == 0  # without --strict a missing optional dependency is reported, not failed


def test_exit_code_precedence(bench):
    assert bench.exit_code({"a": "invalid", "b": "failed"}, strict=True) == 3
    assert bench.exit_code({"a": "failed", "b": "not_run"}, strict=True) == 4
    assert bench.exit_code({"a": "passed", "b": "not_run"}, strict=True) == 5
    assert bench.exit_code({}, strict=False) == 5


def test_script_imports_app_when_run_by_path_from_another_directory(tmp_path):
    """The CI invocation: `python scripts/benchmark.py`, here from an unrelated cwd."""
    out = tmp_path / "ci.json"
    proc = subprocess.run([sys.executable, str(SCRIPT), "--quick", "--strict", "--suite", "alerts",
                           "--suite", "overview", "--out", str(out)],
                          cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(out.read_text())
    assert data["status"] == {"alerts": "passed", "overview": "passed"}


def test_real_duplicate_updates_send_exactly_one_reply(bench, tmp_path):
    pytest.importorskip("aiosqlite")
    pytest.importorskip("aiogram")
    from types import SimpleNamespace

    results = bench.suite_engine(SimpleNamespace(messages=30, concurrency=[1, 10], latency_ms=0.0), tmp_path)
    for conc in (1, 10):
        row = results[f"duplicate_updates@c{conc}"]
        assert row["replies_sent"] == 1 and "INVALID" not in row
        assert row["at_most_once"].startswith("verified")
