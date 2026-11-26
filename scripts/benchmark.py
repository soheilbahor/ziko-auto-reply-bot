#!/usr/bin/env python3
"""ZIKO benchmark harness: repeatable, measured, never guessed.

Run from the installed tree (as the service user or root, never against live data):

    sudo -u ziko /opt/ziko-assistant/.venv/bin/python scripts/benchmark.py --out bench.json
    ... change code ...
    python scripts/benchmark.py --compare bench.json            # exit 1 on regression

Suites (``--suite`` to pick; default all that can run here):

  startup    wall time of a fresh ``import app.main`` (subprocess, N runs)
  engine     the REAL BusinessMessageEngine + Database (aiosqlite, temp file) with a
             fake Telegram sender of configurable latency: throughput, per-message
             latency p50/p95/p99, DB calls per message, at several concurrency levels
  tenants    K tenant databases processed concurrently (multi-tenant throughput)
  overview   platform_overview.collect over N tenant DBs (/platform and alerts)
  telemetry  per-call overhead of the Telegram telemetry middleware
  alerts     AlertEngine.step cost for N bots

Exit codes: 0 every selected suite ran and passed its correctness guards; 1 regression
(--compare); 2 compare refused (different machine); 3 at-most-once guard broken (a duplicate
or missing reply); 4 a suite raised; 5 a suite could not run (missing dependency) and
--strict was given. A suite that did not run is never reported as passed.

Everything uses throwaway files in a private temp directory. Telegram is never
contacted. Results carry the environment (CPU, Python, SQLite, filesystem) because a
number without its machine is meaningless; ``--compare`` refuses to compare runs from
different machines unless ``--force``.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import platform
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
# `python scripts/benchmark.py` puts scripts/ (not the repository) on sys.path, so `app`
# would not be importable and every suite would silently be "not run". Fix the path here.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
FORMAT = "ziko-benchmark/1"
# Metrics compared by --compare, and whether larger is better.
HIGHER_IS_BETTER = {"msgs_per_s": True, "p50_ms": False, "p95_ms": False, "p99_ms": False,
                    "ms": False, "ms_per_bot": False, "us_per_call": False, "db_calls_per_msg": False,
                    "rss_delta_mb": False, "startup_ms": False}


def _mod(name: str) -> Any:
    """Import an app module. Overridable so the harness can be driven from tests."""
    return importlib.import_module(name)


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 3)


def summary(samples_ms: list[float]) -> dict[str, float]:
    return {"n": len(samples_ms), "p50_ms": pct(samples_ms, .50), "p95_ms": pct(samples_ms, .95),
            "p99_ms": pct(samples_ms, .99), "mean_ms": round(statistics.fmean(samples_ms), 3),
            "max_ms": round(max(samples_ms), 3)}


def rss_mb() -> float | None:
    try:
        import psutil  # type: ignore[import-untyped]
        return round(psutil.Process().memory_info().rss / 2**20, 2)
    except Exception:
        try:
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 2)
        except OSError:
            pass
    return None


def environment(workdir: Path) -> dict[str, Any]:
    env: dict[str, Any] = {
        "python": platform.python_version(), "implementation": platform.python_implementation(),
        "sqlite": sqlite3.sqlite_version, "platform": platform.platform(),
        "machine": platform.machine(), "cpus": os.cpu_count(),
    }
    try:
        env["ziko_version"] = (ROOT / "VERSION").read_text().strip()
    except OSError:
        env["ziko_version"] = "unknown"
    # Filesystem fsync cost dominates SQLite write latency: measure it, do not assume it.
    probe = workdir / "fsync.probe"
    times = []
    try:
        with open(probe, "wb") as fh:
            for _ in range(20):
                fh.write(b"x" * 4096)
                t0 = time.perf_counter()
                fh.flush()
                os.fsync(fh.fileno())
                times.append((time.perf_counter() - t0) * 1000)
        env["fsync_p50_ms"] = pct(times, .5)
    except (OSError, AttributeError, PermissionError):
        env["fsync_p50_ms"] = None
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    return env


# --------------------------------------------------------------------------- startup
def suite_startup(args: argparse.Namespace, workdir: Path) -> dict[str, Any]:
    import subprocess
    runs = []
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    for _ in range(args.startup_runs):
        t0 = time.perf_counter()
        proc = subprocess.run([sys.executable, "-c", "import app.main"], cwd=ROOT, env=env,
                              capture_output=True, timeout=120)
        if proc.returncode != 0:
            return {"error": "import app.main failed", "stderr_tail": proc.stderr.decode()[-400:]}
        runs.append((time.perf_counter() - t0) * 1000)
    return {"startup_ms": pct(runs, .5), "runs_ms": [round(r, 1) for r in runs],
            "note": "fresh interpreter + imports; excludes DB open and Telegram getMe"}


# --------------------------------------------------------------------------- engine
class FakeSender:
    """Stands in for aiogram.Bot: records sends, waits ``latency_ms`` like a real API call."""

    def __init__(self, latency_ms: float) -> None:
        self.latency = latency_ms / 1000
        self.sent = 0
        self._mid = 0

    async def _send(self, **_: Any) -> Any:
        if self.latency:
            await asyncio.sleep(self.latency)
        self.sent += 1
        self._mid += 1
        return SimpleNamespace(message_id=self._mid)

    send_message = _send
    send_sticker = _send


class CountingDB:
    """Transparent proxy that counts every awaited storage call."""

    def __init__(self, db: Any) -> None:
        self._db, self.calls = db, 0

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._db, name)
        if not callable(target) or name.startswith("_"):
            return target

        async def wrapped(*a: Any, **k: Any) -> Any:
            self.calls += 1
            return await target(*a, **k)
        return wrapped


OWNER, CONN = 900_000_001, "bench-connection"


def _message(chat_id: int, message_id: int) -> Any:
    return SimpleNamespace(
        business_connection_id=CONN, message_id=message_id, edit_date=None, text="hi",
        date=None, chat=SimpleNamespace(id=chat_id, type="private", first_name="C"),
        from_user=SimpleNamespace(id=chat_id, is_bot=False, first_name="C", username=None,
                                  full_name="C", language_code="fa"),
    )


async def _armed_engine(path: Path, latency_ms: float) -> tuple[Any, CountingDB, FakeSender]:
    storage = _mod("app.storage")
    auto = _mod("app.services.auto_reply")
    db = storage.Database(path)
    await db.initialize()
    await db.save_business_connection(CONN, True, True, OWNER)
    statuses = await db.list_statuses()
    await db.activate_status(statuses[0].key)
    state = await db.get_app_state()
    if not state.enabled:
        await db.enable_auto_reply()
    counting = CountingDB(db)
    sender = FakeSender(latency_ms)
    settings = SimpleNamespace(admin_ids=frozenset({OWNER}), business_owner_id=OWNER)
    engine = auto.BusinessMessageEngine(db=counting, sender=sender, settings=settings)
    engine.step_delay = 0
    return engine, counting, sender


async def _drive(engine: Any, messages: list[Any], concurrency: int) -> list[float]:
    sem = asyncio.Semaphore(concurrency)
    lat: list[float] = []

    async def one(m: Any) -> None:
        async with sem:
            t0 = time.perf_counter()
            await engine.handle_business_message(m)
            lat.append((time.perf_counter() - t0) * 1000)
    await asyncio.gather(*(one(m) for m in messages))
    return lat


def suite_engine(args: argparse.Namespace, workdir: Path) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        out: dict[str, Any] = {"sender_latency_ms": args.latency_ms}
        for scenario in ("new_contacts", "repeat_contacts", "duplicate_updates"):
            for conc in args.concurrency:
                path = workdir / f"engine-{scenario}-{conc}.sqlite3"
                engine, db, sender = await _armed_engine(path, args.latency_ms)
                n = args.messages
                if scenario == "new_contacts":          # every message earns a reply
                    msgs = [_message(10_000 + i, 1) for i in range(n)]
                elif scenario == "repeat_contacts":     # contacts already answered this session
                    await _drive(engine, [_message(10_000 + i, 1) for i in range(50)], 10)
                    msgs = [_message(10_000 + (i % 50), 2 + i) for i in range(n)]
                else:                                   # Telegram re-delivers the same update
                    msgs = [_message(20_000, 1)] * n
                db.calls, sender.sent = 0, 0
                rss0 = rss_mb()
                t0 = time.perf_counter()
                lat = await _drive(engine, msgs, conc)
                wall = time.perf_counter() - t0
                rss1 = rss_mb()
                await db._db.close()
                key = f"{scenario}@c{conc}"
                out[key] = {**summary(lat), "msgs_per_s": round(n / wall, 1),
                            "db_calls_per_msg": round(db.calls / n, 2), "replies_sent": sender.sent,
                            "rss_delta_mb": round(rss1 - rss0, 2) if rss0 and rss1 else None}
                # Correctness guard: a benchmark that breaks at-most-once is not a result.
                expected = {"new_contacts": n, "repeat_contacts": 0, "duplicate_updates": 1}[scenario]
                if sender.sent != expected:
                    out[key]["INVALID"] = f"sent {sender.sent} replies, expected {expected}"
                else:
                    out[key]["at_most_once"] = f"verified: {sender.sent} sent, {expected} expected"
        return out
    return asyncio.run(run())


def suite_tenants(args: argparse.Namespace, workdir: Path) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        out = {}
        for k in args.tenants:
            engines = [await _armed_engine(workdir / f"tenant-{k}-{i}.sqlite3", args.latency_ms)
                       for i in range(k)]
            per = max(1, args.messages // k)
            t0 = time.perf_counter()
            lats = await asyncio.gather(*(
                _drive(e, [_message(30_000 + j, 1) for j in range(per)], 10) for e, _, _ in engines))
            wall = time.perf_counter() - t0
            flat = [x for per_tenant in lats for x in per_tenant]
            for _, db, _ in engines:
                await db._db.close()
            out[f"tenants={k}"] = {**summary(flat), "msgs_per_s": round(len(flat) / wall, 1)}
        return out
    return asyncio.run(run())


# --------------------------------------------------------------------------- overview
def build_tenant_dbs(data: Path, n: int) -> list[Any]:
    schema = (ROOT / "app" / "database_schema.sql").read_text()
    (data / "tenants").mkdir(parents=True, exist_ok=True)
    now = "2026-01-01T00:00:00.000Z"
    tenants = []
    for i in range(n):
        mem = sqlite3.connect(":memory:")
        mem.executescript(schema)
        mem.execute("INSERT INTO statuses(key,title,emoji,reply_text,updated_at) VALUES('away','Away','🚶','hi',?)", (now,))
        mem.execute("INSERT INTO owner_settings(id,updated_at) VALUES(1,?)", (now,))
        mem.execute("INSERT INTO app_state(id,enabled,active_status_key,session_id,updated_at) VALUES(1,1,'away','s',?)", (now,))
        mem.execute("INSERT INTO business_connections VALUES('c',1,1,1,?,?)", (now, now))
        for j in range(50):
            mem.execute("INSERT INTO reply_deliveries(connection_id,chat_id,session_id,state,claim_token,claimed_at,"
                        "completed_at,updated_at) VALUES('c',?,'s','sent','t',?,?,?)", (j, now, now, now))
        mem.commit()
        (data / "tenants" / f"bot-{1000 + i}.sqlite3").write_bytes(mem.serialize())
        mem.close()
        tenants.append(SimpleNamespace(user_id=5000 + i, bot_id=1000 + i, username=f"bench{i}", enabled=True))
    return tenants


def suite_overview(args: argparse.Namespace, workdir: Path) -> dict[str, Any]:
    po = _mod("app.platform_overview")
    out = {}
    for n in args.bots:
        data = workdir / f"overview-{n}"
        tenants = build_tenant_dbs(data, n)
        tel = {t.bot_id: {"errors": {}, "last_success_at": time.time(), "latency_ms": {}} for t in tenants}
        runs = []
        for _ in range(args.repeat):
            t0 = time.perf_counter()
            ov = po.collect(tenants=tenants, data_dir=data, runtime_of=lambda _b: "running",
                            telemetry=tel, running=n, waiting=0, limit=max(n, 1))
            runs.append((time.perf_counter() - t0) * 1000)
        assert len(ov.bots) == n
        ms = pct(runs, .5)
        out[f"bots={n}"] = {"ms": ms, "ms_per_bot": round(ms / n, 3), "runs_ms": [round(r, 1) for r in runs]}
        shutil.rmtree(data, ignore_errors=True)
    return out


# --------------------------------------------------------------------------- telemetry
def suite_telemetry(args: argparse.Namespace, workdir: Path) -> dict[str, Any]:
    ts = _mod("app.telegram_stats")

    class SendMessage:
        pass

    async def ok(_bot: Any, _m: Any) -> int:
        return 1

    async def run() -> dict[str, Any]:
        stats = ts.TelegramStats()
        mw = ts.TelemetryRequestMiddleware(1, "child", stats)
        n = args.calls
        m = SendMessage()
        t0 = time.perf_counter()
        for _ in range(n):
            await ok(None, m)
        base = time.perf_counter() - t0
        t0 = time.perf_counter()
        for _ in range(n):
            await mw(ok, None, m)
        with_mw = time.perf_counter() - t0
        t0 = time.perf_counter()
        for _ in range(200):
            stats.snapshot()
        snap = (time.perf_counter() - t0) / 200
        return {"us_per_call": round((with_mw - base) / n * 1e6, 3), "calls": n,
                "snapshot_us": round(snap * 1e6, 1)}
    return asyncio.run(run())


def suite_alerts(args: argparse.Namespace, workdir: Path) -> dict[str, Any]:
    pa = _mod("app.platform_alerts")
    out = {}
    for n in args.bots:
        e = pa.AlertEngine(pa.AlertConfig(confirm_checks=2))
        bots = [SimpleNamespace(bot_id=i, username=f"b{i}", state="crashed" if i % 7 == 0 else "ready",
                                reasons=["x"]) for i in range(n)]
        runs = []
        for k in range(max(args.repeat, 5)):
            t0 = time.perf_counter()
            e.step(bots, waiting=0, running=n, limit=n, now=1e9 + k * 60)
            runs.append((time.perf_counter() - t0) * 1000)
        out[f"bots={n}"] = {"ms": pct(runs, .5)}
    return out


SUITES: dict[str, tuple[Callable[..., dict[str, Any]], tuple[str, ...]]] = {
    "startup": (suite_startup, ("app.main",)),
    "engine": (suite_engine, ("aiosqlite", "aiogram", "app.storage", "app.services.auto_reply")),
    "tenants": (suite_tenants, ("aiosqlite", "aiogram", "app.storage", "app.services.auto_reply")),
    "overview": (suite_overview, ("app.platform_overview",)),
    "telemetry": (suite_telemetry, ("app.telegram_stats",)),
    "alerts": (suite_alerts, ("app.platform_alerts",)),
}


def _available(deps: tuple[str, ...]) -> str | None:
    for d in deps:
        try:
            _mod(d)
        except Exception as exc:
            return f"{d}: {type(exc).__name__}"
    return None


# --------------------------------------------------------------------------- compare
def _metrics(results: dict[str, Any], prefix: str = "") -> dict[str, float]:
    flat: dict[str, float] = {}
    for k, v in results.items():
        if isinstance(v, dict):
            flat.update(_metrics(v, f"{prefix}{k}."))
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and k in HIGHER_IS_BETTER:
            flat[f"{prefix}{k}"] = float(v)
    return flat


def compare(base: dict[str, Any], cur: dict[str, Any], threshold: float, force: bool) -> tuple[int, list[str]]:
    lines: list[str] = []
    same_machine = all(base["environment"].get(k) == cur["environment"].get(k)
                       for k in ("machine", "cpus", "python", "sqlite"))
    if not same_machine and not force:
        return 2, ["refusing to compare: different machine/python/sqlite (use --force)"]
    old, new = _metrics(base["results"]), _metrics(cur["results"])
    worse = 0
    for key in sorted(old.keys() & new.keys()):
        a, b_ = old[key], new[key]
        if a == 0:
            continue
        change = (b_ - a) / a * 100
        better_up = HIGHER_IS_BETTER[key.rsplit(".", 1)[-1]]
        regressed = change < -threshold if better_up else change > threshold
        improved = change > threshold if better_up else change < -threshold
        tag = "REGRESSION" if regressed else "improved" if improved else "same"
        worse += regressed
        lines.append(f"{tag:<10} {key:<52} {a:>12.3f} -> {b_:>12.3f} ({change:+.1f}%)")
    return (1 if worse else 0), lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--suite", action="append", choices=sorted(SUITES), help="repeatable; default: all available")
    p.add_argument("--out", type=Path, help="write JSON results here")
    p.add_argument("--compare", type=Path, metavar="BASELINE", help="compare with an earlier --out file")
    p.add_argument("--threshold", type=float, default=15.0, help="regression threshold in %% (default 15)")
    p.add_argument("--force", action="store_true", help="compare even across machines")
    p.add_argument("--quick", action="store_true", help="small sizes for a smoke run")
    p.add_argument("--strict", action="store_true",
                   help="exit 5 if any selected suite could not run (CI uses this)")
    # Defaults are None so --quick only fills in sizes the user did not set explicitly.
    p.add_argument("--messages", type=int)
    p.add_argument("--concurrency", type=int, nargs="+")
    p.add_argument("--latency-ms", type=float, default=0.0, help="fake Telegram latency per send")
    p.add_argument("--tenants", type=int, nargs="+")
    p.add_argument("--bots", type=int, nargs="+")
    p.add_argument("--repeat", type=int)
    p.add_argument("--calls", type=int)
    p.add_argument("--startup-runs", type=int)
    args = p.parse_args(argv)
    sizes = (dict(messages=200, concurrency=[1, 10], tenants=[1, 5], bots=[10, 50], repeat=2,
                  calls=5000, startup_runs=2) if args.quick else
             dict(messages=2000, concurrency=[1, 10, 50], tenants=[1, 10, 50], bots=[10, 100, 500],
                  repeat=3, calls=50_000, startup_runs=5))
    for name, value in sizes.items():
        if getattr(args, name) is None:
            setattr(args, name, value)
    workdir = Path(tempfile.mkdtemp(prefix="ziko-bench-"))
    os.chmod(workdir, 0o700)
    if args.out is not None:
        args.out = args.out.resolve()
        if workdir.resolve() in args.out.parents:
            p.error("--out must not be inside the temporary work directory (it is deleted)")
        args.out.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"format": FORMAT, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              "environment": environment(workdir), "args": {k: v for k, v in vars(args).items()
                                                                            if k not in {"out", "compare"}},
                              "results": {}, "skipped": {}, "failed": {}, "status": {}}
    try:
        for name in args.suite or list(SUITES):
            fn, deps = SUITES[name]
            missing = _available(deps)
            if missing:
                report["skipped"][name] = f"not run: {missing}"
                report["status"][name] = "not_run"
                print(f"SKIP {name:<10} {missing}", flush=True)
                continue
            t0 = time.perf_counter()
            try:
                result = fn(args, workdir)
            except Exception as exc:  # a failing suite is reported, never silently dropped
                report["failed"][name] = f"{type(exc).__name__}: {exc}"
                report["status"][name] = "failed"
                print(f"FAIL {name:<10} {type(exc).__name__}: {exc}", flush=True)
                continue
            report["results"][name] = result
            invalid = _invalid_rows(result)
            report["status"][name] = "invalid" if invalid else "passed"
            label = "INVALID" if invalid else "DONE"
            print(f"{label:<4} {name:<10} {time.perf_counter() - t0:6.1f}s", flush=True)
            for key, why in invalid:
                print(f"     {key}: {why}", flush=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    rc = exit_code(report["status"], strict=args.strict)
    report["exit_code"] = rc
    print(json.dumps(report["results"], indent=2, ensure_ascii=False))
    counts = {s: sum(1 for v in report["status"].values() if v == s)
              for s in ("passed", "invalid", "failed", "not_run")}
    print("SUMMARY " + " ".join(f"{k}={v}" for k, v in counts.items()) + f" exit={rc}", flush=True)
    if args.out:
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    if rc == 0 and args.compare:
        rc, lines = compare(json.loads(args.compare.read_text()), report, args.threshold, args.force)
        print("\n".join(lines))
    return rc


def _invalid_rows(result: Any) -> list[tuple[str, str]]:
    if not isinstance(result, dict):
        return []
    return [(k, v["INVALID"]) for k, v in result.items() if isinstance(v, dict) and "INVALID" in v]


def exit_code(status: dict[str, str], *, strict: bool) -> int:
    """Correctness first: a broken guard or a crash always fails; "not run" never passes."""
    values = set(status.values())
    if "invalid" in values:
        return 3
    if "failed" in values:
        return 4
    if strict and "not_run" in values:
        return 5
    if not status:
        return 5  # nothing selected ran at all
    return 0


if __name__ == "__main__":
    sys.exit(main())
