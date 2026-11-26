"""Telegram diagnostics for the CLI (``ziko monitor telegram``, ``ziko bots inspect``).

Checks, in order of cost:
1. DNS resolution of api.telegram.org
2. TCP connect time to port 443
3. TLS handshake with full certificate + hostname verification (reports protocol)
4. Unauthenticated HTTPS round trip (no token is sent)
5. Master bot getMe / getWebhookInfo (token used, never displayed)
6. Live telemetry exported by the running service (latency, failures, 429s,
   last success per bot) -- stale data is flagged, never shown as current
7. Business connections per tenant database (read-only)
8. Telegram-related error lines in the rotated log files (counts only)

Child-bot tokens are only decrypted when ``probe_children=True`` is requested.
"""
from __future__ import annotations

import re
import socket
import sqlite3
import ssl
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app import telegram_stats

API_HOST = "api.telegram.org"
STALE_SECONDS = 60
Check = tuple[str, str, str]  # (level ok|warn|fail|info, name, detail)

_LOG_PATTERNS = {
    "rate_limited": re.compile(r"Too Many Requests|retry after|TelegramRetryAfter|Flood", re.I),
    "conflict": re.compile(r"409|Conflict|terminated by other getUpdates", re.I),
    "unauthorized": re.compile(r"Unauthorized|token was rejected", re.I),
    "network": re.compile(r"TelegramNetworkError|ClientConnector|timed? ?out|Cannot connect", re.I),
    "server_error": re.compile(r"TelegramServerError|Bad Gateway|50[234]", re.I),
}


def network_checks(host: str = API_HOST, timeout: float = 5.0) -> tuple[list[Check], dict[str, Any]]:
    checks: list[Check] = []
    m: dict[str, Any] = {}
    t0 = time.perf_counter()
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        m["dns_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        addr = infos[0][4]
        checks.append(("ok", "DNS", f"{host} → {addr[0]} in {m['dns_ms']} ms"))
    except OSError as exc:
        checks.append(("fail", "DNS", f"{host} did not resolve ({type(exc).__name__})"))
        return checks, m
    t1 = time.perf_counter()
    try:
        sock = socket.create_connection(addr[:2], timeout=timeout)
    except OSError as exc:
        checks.append(("fail", "TCP 443", f"connect failed ({type(exc).__name__}); firewall or routing?"))
        return checks, m
    m["tcp_ms"] = round((time.perf_counter() - t1) * 1000, 1)
    checks.append(("ok", "TCP 443", f"connected in {m['tcp_ms']} ms"))
    ctx = ssl.create_default_context()  # verifies chain + hostname
    t2 = time.perf_counter()
    try:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            m["tls_ms"] = round((time.perf_counter() - t2) * 1000, 1)
            m["tls_version"] = tls.version()
            cert = tls.getpeercert() or {}
            expires = cert.get("notAfter")
            days = round((ssl.cert_time_to_seconds(expires) - time.time()) / 86400) if expires else None
            level = "ok" if m["tls_version"] == "TLSv1.3" else "warn"
            checks.append((level, "TLS", f"{m['tls_version']}, certificate verified, "
                                         f"expires in {days} days, {m['tls_ms']} ms"))
            t3 = time.perf_counter()
            tls.sendall(f"HEAD / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            status = tls.recv(64).split(b"\r\n", 1)[0].decode("ascii", "replace")
            m["https_ms"] = round((time.perf_counter() - t3) * 1000, 1)
            checks.append(("ok", "HTTPS round trip", f"{status} in {m['https_ms']} ms (no token sent)"))
    except ssl.SSLCertVerificationError:
        checks.append(("fail", "TLS", "certificate verification FAILED — possible interception (MITM proxy)"))
    except (ssl.SSLError, OSError) as exc:
        checks.append(("fail", "TLS", f"handshake failed ({type(exc).__name__})"))
    return checks, m


def master_checks(token: str, api: Callable[..., tuple[bool, Any]]) -> list[Check]:
    """``api`` is cli._telegram_api, which already guarantees no token in errors."""
    out: list[Check] = []
    t0 = time.perf_counter()
    ok, me = api(token, "getMe")
    ms = round((time.perf_counter() - t0) * 1000)
    if not ok:
        out.append(("fail", "Master bot auth", f"getMe failed: {me}"))
        return out
    out.append(("ok", "Master bot auth", f"@{me.get('username')} id={me.get('id')} · getMe {ms} ms"))
    if not me.get("can_connect_to_business", True):
        out.append(("warn", "Business mode", "Business Mode is OFF for this bot — enable it in @BotFather"))
    ok, hook = api(token, "getWebhookInfo")
    if ok and isinstance(hook, dict):
        if hook.get("url"):
            out.append(("warn", "Delivery mode", "a webhook is set but ZIKO uses polling — "
                        "polling will receive nothing (run deleteWebhook)"))
        else:
            pending = hook.get("pending_update_count", 0)
            out.append(("ok" if pending < 100 else "warn", "Delivery mode",
                        f"long polling · {pending} pending updates"))
    return out


def telemetry_checks(status_path: Path, now: float | None = None) -> tuple[list[Check], list[dict[str, Any]]]:
    now = now or time.time()
    data = telegram_stats.read_status(status_path)
    if data is None:
        return [("warn", "Live telemetry", "no status file — service stopped or older version")], []
    age = now - float(data.get("written_at", 0))
    bots = data.get("bots", [])
    if age > STALE_SECONDS:
        return [("warn", "Live telemetry", f"STALE: last written {int(age)} s ago "
                 f"(pid {data.get('pid')}) — values below are not current")], bots
    checks: list[Check] = [("ok", "Live telemetry", f"{len(bots)} bots reporting · updated {int(age)} s ago")]
    for b in bots:
        name = f"{b['role']} {b['bot_id']}"
        last_ok = b.get("last_success_at")
        silent = now - last_ok if last_ok else None
        fail_rate = b["failures"] / b["requests"] if b["requests"] else 0
        if last_ok is None:
            lvl, why = "fail", "no successful API call yet"
        elif silent is not None and silent > 120:
            lvl, why = "fail", f"no successful call for {int(silent)} s"
        elif fail_rate > 0.05 or b["errors"].get("unauthorized"):
            lvl, why = "warn", f"failure rate {fail_rate:.1%}"
        else:
            lvl, why = "ok", "healthy"
        lat = b["latency_ms"]
        rl = f" · 429×{b['rate_limited']} (last retry_after {b['last_retry_after']} s)" if b["rate_limited"] else ""
        errs = ", ".join(f"{k}×{v}" for k, v in b["errors"].items()) or "none"
        checks.append((lvl, name, f"{why} · p50 {lat['p50']} / p95 {lat['p95']} ms · "
                       f"{b['requests']} req · errors: {errs}{rl}"))
    return checks, bots


def business_connections(db_path: Path) -> dict[str, int] | None:
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(is_enabled),0), "
                "COALESCE(SUM(is_enabled AND can_reply),0) FROM business_connections").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return {"total": row[0], "enabled": row[1], "can_reply": row[2]}


def business_checks(db_paths: dict[str, Path]) -> list[Check]:
    out: list[Check] = []
    for label, path in db_paths.items():
        if label == "registry":
            continue
        bc = business_connections(path)
        if bc is None:
            continue
        if bc["enabled"] == 0:
            lvl, msg = "info", "no active Business connection (owner has not connected)"
        elif bc["can_reply"] == 0:
            lvl, msg = "warn", "connected but WITHOUT reply permission — grant 'reply to messages'"
        else:
            lvl, msg = "ok", f"{bc['can_reply']} connection(s) able to reply"
        out.append((lvl, f"Business {label}", msg))
    return out


def scan_logs(log_dir: Path, max_bytes: int = 2_000_000) -> dict[str, Counter[str]]:
    """Count Telegram error categories per log file; reads only the tail of each file."""
    result: dict[str, Counter[str]] = {}
    if not log_dir.is_dir():
        return result
    files = [log_dir / "master.log", *sorted((log_dir / "tenants").glob("bot-*.log"))]
    for f in files:
        try:
            with f.open("rb") as fh:
                fh.seek(max(0, f.stat().st_size - max_bytes))
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        c: Counter[str] = Counter()
        for line in text.splitlines():
            if " WARNING " not in line and " ERROR " not in line and " CRITICAL " not in line:
                continue
            for cat, pat in _LOG_PATTERNS.items():
                if pat.search(line):
                    c[cat] += 1
        if c:
            result[f.name] = c
    return result


def log_checks(log_dir: Path) -> list[Check]:
    counts = scan_logs(log_dir)
    if not counts:
        return [("ok", "Telegram errors in logs", "none found in recent log tails")]
    out = []
    for name, c in counts.items():
        lvl = "fail" if c.get("unauthorized") or c.get("conflict") else "warn"
        out.append((lvl, f"log {name}", ", ".join(f"{k}×{v}" for k, v in c.most_common())))
    return out


def probe_child(token: str, api: Callable[..., tuple[bool, Any]]) -> Check:
    t0 = time.perf_counter()
    ok, me = api(token, "getMe")
    ms = round((time.perf_counter() - t0) * 1000)
    if ok:
        return ("ok", "token", f"valid · @{me.get('username')} · {ms} ms")
    return ("fail", "token", f"rejected or unreachable: {me}")


def overall(checks: list[Check]) -> int:
    levels = {c[0] for c in checks}
    return 2 if "fail" in levels else 1 if "warn" in levels else 0
