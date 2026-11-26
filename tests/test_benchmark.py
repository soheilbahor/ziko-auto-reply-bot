"""Benchmark harness logic, plus a real-engine at-most-once check (scripts/benchmark.py)."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def bench():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "benchmark.py"
    spec = importlib.util.spec_from_file_location("ziko_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def report(env=None, **results):
    return {"environment": env or {"machine": "x86_64", "cpus": 4, "python": "3.12", "sqlite": "3.45"},
            "results": results}


def test_percentiles(bench):
    data = list(range(1, 101))
    assert bench.pct(data, 0.5) == 50.5 and bench.pct(data, 0.95) == 95.05
    assert bench.pct([7.0], 0.99) == 7.0
    s = bench.summary([1, 2, 3, 4, 100])
    assert s["p50_ms"] == 3 and s["max_ms"] == 100 and s["n"] == 5


def test_compare_directions_threshold_and_machine(bench):
    base = report(overview={"bots=30": {"ms": 100.0}}, engine={"x": {"msgs_per_s": 100.0}})
    assert bench.compare(base, copy.deepcopy(base), 15, False)[0] == 0
    slower = copy.deepcopy(base)
    slower["results"]["overview"]["bots=30"]["ms"] = 200.0
    rc, lines = bench.compare(base, slower, 15, False)
    assert rc == 1 and any(line.startswith("REGRESSION") and "overview.bots=30.ms" in line for line in lines)
    small = copy.deepcopy(base)
    small["results"]["overview"]["bots=30"]["ms"] = 110.0
    assert bench.compare(base, small, 15, False)[0] == 0
    drop = copy.deepcopy(base)
    drop["results"]["engine"]["x"]["msgs_per_s"] = 50.0
    assert bench.compare(base, drop, 15, False)[0] == 1     # throughput drop is a regression
    assert bench.compare(drop, base, 15, False)[0] == 0     # throughput rise is not
    other = copy.deepcopy(base)
    other["environment"]["cpus"] = 999
    assert bench.compare(base, other, 15, False)[0] == 2
    assert bench.compare(base, other, 15, True)[0] == 0


def test_quick_run_keeps_explicit_sizes_and_writes_report(bench, tmp_path):
    out = tmp_path / "results" / "r.json"
    rc = bench.main(["--quick", "--suite", "alerts", "--suite", "telemetry", "--bots", "10", "30",
                     "--out", str(out)])
    data = json.loads(out.read_text())
    assert rc == 0 and data["format"] == "ziko-benchmark/1"
    assert data["args"]["bots"] == [10, 30] and data["args"]["messages"] == 200
    assert set(data["results"]) == {"alerts", "telemetry"}
    assert 0 < data["results"]["telemetry"]["us_per_call"] < 1000


def test_real_engine_keeps_at_most_once_under_concurrency(bench, tmp_path):
    pytest.importorskip("aiosqlite")
    pytest.importorskip("aiogram")
    args = SimpleNamespace(messages=40, concurrency=[1, 10], latency_ms=0.0)
    results = bench.suite_engine(args, tmp_path)
    for key, row in results.items():
        if isinstance(row, dict):
            assert "INVALID" not in row, (key, row)
    assert results["new_contacts@c10"]["replies_sent"] == 40
    assert results["duplicate_updates@c10"]["replies_sent"] == 1
    assert results["repeat_contacts@c10"]["replies_sent"] == 0
