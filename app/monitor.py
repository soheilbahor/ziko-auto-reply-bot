"""Read-only server metrics for the ZIKO CLI.

Rules:
* Every value comes from the operating system (psutil, or /proc as a fallback).
* A metric that cannot be measured is reported as ``None`` with a reason in
  ``unsupported`` -- it is never estimated or invented.
* Nothing here changes system state, opens network sockets to Telegram, or
  reads secrets. Process command lines are not collected (they may contain tokens).
"""
from __future__ import annotations

import os
import shutil
import socket
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:  # psutil is optional: the CLI degrades to /proc instead of failing.
    import psutil  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - exercised only on hosts without psutil
    psutil = None  # type: ignore[assignment]


@dataclass(slots=True)
class Snapshot:
    taken_at: float
    section: str
    data: dict[str, Any] = field(default_factory=dict)
    unsupported: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_METRIC_ERRORS: tuple[type[BaseException], ...] = (OSError, PermissionError, RuntimeError)
if psutil is not None:
    _METRIC_ERRORS = _METRIC_ERRORS + (psutil.Error,)


def _guarded(section: str):
    """Never let one unreadable OS source crash the CLI: report it as unsupported."""
    def wrap(fn):
        def inner(*args, **kwargs) -> Snapshot:
            try:
                return fn(*args, **kwargs)
            except _METRIC_ERRORS as exc:
                snap = Snapshot(time.time(), section)
                snap.unsupported["all"] = f"OS metrics not readable here ({type(exc).__name__})"
                return snap
        inner.__name__ = fn.__name__
        inner.__doc__ = fn.__doc__
        return inner
    return wrap


def _proc_cpu_times() -> tuple[int, int] | None:
    try:
        parts = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        values = [int(v) for v in parts]
    except (OSError, ValueError, IndexError):
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


@_guarded("cpu")
def cpu(sample_seconds: float = 0.25, top: int = 5) -> Snapshot:
    snap = Snapshot(time.time(), "cpu")
    d, u = snap.data, snap.unsupported
    if psutil is not None:
        per_core = psutil.cpu_percent(interval=sample_seconds, percpu=True)
        d["per_core_percent"] = per_core
        d["percent"] = round(sum(per_core) / len(per_core), 1) if per_core else None
        d["logical_cores"] = psutil.cpu_count(logical=True)
        d["physical_cores"] = psutil.cpu_count(logical=False)
        try:
            freq = psutil.cpu_freq()
            d["frequency_mhz"] = round(freq.current, 0) if freq else None
            if not freq:
                u["frequency_mhz"] = "not exposed by this kernel/virtualization"
        except (OSError, NotImplementedError):
            d["frequency_mhz"] = None
            u["frequency_mhz"] = "not exposed by this kernel/virtualization"
        temps = getattr(psutil, "sensors_temperatures", lambda: {})() or {}
        readings = [t.current for entries in temps.values() for t in entries if t.current]
        d["temperature_c"] = round(max(readings), 1) if readings else None
        if not readings:
            u["temperature_c"] = "no hardware sensors visible (common on VPS)"
        d["top_processes"] = _top_processes("cpu_percent", top)
    else:
        a = _proc_cpu_times()
        time.sleep(sample_seconds)
        b = _proc_cpu_times()
        if a and b and b[0] > a[0]:
            d["percent"] = round(100.0 * (1 - (b[1] - a[1]) / (b[0] - a[0])), 1)
        else:
            d["percent"] = None
            u["percent"] = "/proc/stat unavailable"
        d["logical_cores"] = os.cpu_count()
        for key in ("per_core_percent", "frequency_mhz", "temperature_c", "top_processes"):
            d[key] = None
            u[key] = "install psutil for this metric"
    try:
        d["load_average"] = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        d["load_average"] = None
        u["load_average"] = "not supported on this platform"
    return snap


@_guarded("memory")
def memory(top: int = 5) -> Snapshot:
    snap = Snapshot(time.time(), "memory")
    d, u = snap.data, snap.unsupported
    if psutil is not None:
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        d.update(total=vm.total, available=vm.available, used=vm.used, percent=vm.percent,
                 cached=getattr(vm, "cached", None), buffers=getattr(vm, "buffers", None),
                 swap_total=sw.total, swap_used=sw.used, swap_percent=sw.percent)
        d["top_processes"] = _top_processes("memory_info", top)
    else:
        info = _meminfo()
        if not info:
            u["all"] = "neither psutil nor /proc/meminfo available"
            return snap
        total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
        d.update(total=total, available=avail, used=total - avail,
                 percent=round(100 * (total - avail) / total, 1) if total else None,
                 cached=info.get("Cached"), swap_total=info.get("SwapTotal"),
                 swap_used=(info.get("SwapTotal", 0) - info.get("SwapFree", 0)))
        u["top_processes"] = "install psutil for this metric"
    d["pressure"] = _psi("memory")
    if d["pressure"] is None:
        u["pressure"] = "kernel PSI (/proc/pressure) not enabled"
    return snap


def _meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            out[key] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return {}
    return out


def _psi(resource: str) -> dict[str, float] | None:
    """Linux pressure-stall information: % of time tasks were stalled (avg10/60/300)."""
    try:
        line = Path(f"/proc/pressure/{resource}").read_text().splitlines()[0]
    except (OSError, IndexError):
        return None
    vals = dict(p.split("=") for p in line.split()[1:] if "=" in p)
    try:
        return {k: float(vals[k]) for k in ("avg10", "avg60", "avg300")}
    except (KeyError, ValueError):
        return None


@_guarded("disk")
def disk(paths: list[Path], sample_seconds: float = 0.25) -> Snapshot:
    snap = Snapshot(time.time(), "disk")
    d, u = snap.data, snap.unsupported
    usage = []
    for p in paths:
        try:
            total, used, free = shutil.disk_usage(p)
        except OSError:
            continue
        usage.append({"path": str(p), "total": total, "used": used, "free": free,
                      "percent": round(100 * used / total, 1) if total else None})
    d["filesystems"] = usage
    if psutil is not None:
        a = psutil.disk_io_counters()
        time.sleep(sample_seconds)
        b = psutil.disk_io_counters()
        if a and b:
            dt = sample_seconds
            d["read_bytes_per_s"] = round((b.read_bytes - a.read_bytes) / dt)
            d["write_bytes_per_s"] = round((b.write_bytes - a.write_bytes) / dt)
            d["iops"] = round(((b.read_count - a.read_count) + (b.write_count - a.write_count)) / dt, 1)
            ops = (b.read_count - a.read_count) + (b.write_count - a.write_count)
            busy = (b.read_time - a.read_time) + (b.write_time - a.write_time)
            d["avg_latency_ms"] = round(busy / ops, 2) if ops else 0.0
        else:
            u["throughput"] = "disk counters not exposed (container or VM)"
    else:
        u["throughput"] = "install psutil for this metric"
    d["io_pressure"] = _psi("io")
    return snap


def dir_size(path: Path, max_entries: int = 200_000) -> int | None:
    """Size of a directory tree, bounded so a huge log dir cannot hang the CLI."""
    total, seen = 0, 0
    try:
        for root, _dirs, files in os.walk(path, followlinks=False):
            for name in files:
                seen += 1
                if seen > max_entries:
                    return None
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    pass
    except OSError:
        return None
    return total


@_guarded("network")
def network(sample_seconds: float = 0.5, dns_host: str = "api.telegram.org") -> Snapshot:
    snap = Snapshot(time.time(), "network")
    d, u = snap.data, snap.unsupported
    if psutil is not None:
        a = psutil.net_io_counters(pernic=True)
        time.sleep(sample_seconds)
        b = psutil.net_io_counters(pernic=True)
        ifaces = []
        for name, now in b.items():
            before = a.get(name)
            if before is None or name == "lo":
                continue
            ifaces.append({
                "name": name,
                "rx_bytes_per_s": round((now.bytes_recv - before.bytes_recv) / sample_seconds),
                "tx_bytes_per_s": round((now.bytes_sent - before.bytes_sent) / sample_seconds),
                "errors_in": now.errin, "errors_out": now.errout,
                "drops_in": now.dropin, "drops_out": now.dropout,
            })
        d["interfaces"] = ifaces
        try:
            states = Counter(c.status for c in psutil.net_connections(kind="tcp"))
            d["tcp_states"] = dict(states)
        except (psutil.AccessDenied, PermissionError, OSError):
            d["tcp_states"] = None
            u["tcp_states"] = "requires root to list sockets"
    else:
        u["interfaces"] = "install psutil for this metric"
    t0 = time.perf_counter()
    try:
        addrs = sorted({ai[4][0] for ai in socket.getaddrinfo(dns_host, 443, type=socket.SOCK_STREAM)})
        d["dns"] = {"host": dns_host, "ok": True, "ms": round((time.perf_counter() - t0) * 1000, 1),
                    "addresses": addrs[:4]}
    except OSError as exc:
        d["dns"] = {"host": dns_host, "ok": False, "error": type(exc).__name__}
    return snap


def _top_processes(metric: str, n: int) -> list[dict[str, Any]]:
    """Top-N processes. Only name/pid/user are collected -- never the command line."""
    if psutil is None:
        return []
    procs = list(psutil.process_iter(["pid", "name", "username", "create_time"]))
    if metric == "cpu_percent":
        for p in procs:
            try:
                p.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        time.sleep(0.2)
    rows = []
    for p in procs:
        try:
            if metric == "cpu_percent":
                value = p.cpu_percent(None)
            else:
                value = p.memory_info().rss
            rows.append({"pid": p.info["pid"], "name": p.info["name"], "user": p.info["username"],
                         "uptime_s": round(time.time() - (p.info["create_time"] or time.time())),
                         metric: value})
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    rows.sort(key=lambda r: r[metric] or 0, reverse=True)
    return rows[:n]


@_guarded("processes")
def processes(name_hint: str = "ziko", top: int = 10) -> Snapshot:
    snap = Snapshot(time.time(), "processes")
    if psutil is None:
        snap.unsupported["all"] = "install psutil for this metric"
        return snap
    snap.data["top_cpu"] = _top_processes("cpu_percent", top)
    snap.data["top_memory"] = _top_processes("memory_info", top)
    ziko = []
    for p in psutil.process_iter(["pid", "name", "username", "create_time"]):
        try:
            cmd = p.cmdline()
            if any("app.main" in part for part in cmd) or name_hint in (p.info["name"] or ""):
                ziko.append({"pid": p.pid, "user": p.info["username"],
                             "rss": p.memory_info().rss, "threads": p.num_threads(),
                             "open_files": len(p.open_files()),
                             "uptime_s": round(time.time() - p.info["create_time"])})
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    snap.data["ziko_processes"] = ziko
    return snap


def uptime() -> float | None:
    if psutil is not None:
        try:
            return time.time() - psutil.boot_time()
        except _METRIC_ERRORS:
            return None
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


@dataclass(slots=True)
class Threshold:
    metric: str
    warn: float
    crit: float


DEFAULT_THRESHOLDS = (
    Threshold("cpu.percent", 85, 95),
    Threshold("memory.percent", 85, 95),
    Threshold("memory.swap_percent", 50, 80),
    Threshold("disk.percent", 85, 95),
)


def evaluate(cpu_s: Snapshot, mem_s: Snapshot, disk_s: Snapshot) -> list[tuple[str, str, str]]:
    """Return (level, metric, message) alerts. level is OK / WARN / CRIT."""
    values: dict[str, float | None] = {
        "cpu.percent": cpu_s.data.get("percent"),
        "memory.percent": mem_s.data.get("percent"),
        "memory.swap_percent": mem_s.data.get("swap_percent"),
        "disk.percent": max((f["percent"] or 0 for f in disk_s.data.get("filesystems", [])), default=None),
    }
    out = []
    for t in DEFAULT_THRESHOLDS:
        v = values.get(t.metric)
        if v is None:
            continue
        level = "CRIT" if v >= t.crit else "WARN" if v >= t.warn else "OK"
        out.append((level, t.metric, f"{v:.1f}% (warn {t.warn:.0f}%, crit {t.crit:.0f}%)"))
    pressure = mem_s.data.get("pressure") or {}
    if pressure.get("avg60", 0) >= 10:
        out.append(("WARN", "memory.pressure", f"tasks stalled {pressure['avg60']}% of last 60s"))
    return out
