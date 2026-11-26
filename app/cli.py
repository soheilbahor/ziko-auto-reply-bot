"""ZIKO command line manager (``ziko``).

Service control uses systemctl; backup/restore/update/uninstall are delegated to the
scripts in ``<ZIKO_HOME>/scripts/``. This module never touches the Telegram engine or the
database contract, and it never prints the bot token: secrets are masked in ``config`` and
redacted from every command output that passes through it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

HOME = Path(os.environ.get("ZIKO_HOME", "/opt/ziko-assistant"))
SERVICE = os.environ.get("ZIKO_SERVICE", "ziko")
SERVICE_USER = "ziko"
def _configured_github_repo() -> str:
    """Resolve repo: environment override, deploy/release.conf, then safe default."""
    env_repo = os.environ.get("ZIKO_REPO", "").strip()
    if env_repo:
        return env_repo
    config = HOME / "deploy" / "release.conf"
    try:
        for raw in config.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("ZIKO_REPO="):
                value = line.partition("=")[2].strip().strip("\\\"' ")
                if value:
                    return value
    except OSError:
        pass
    return "ziko-freedom/ziko-auto-reply-bot"


GITHUB_REPO = _configured_github_repo()
_UPDATE_STATUS_CACHE: tuple[float, dict[str, str | bool]] | None = None

_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")
_TOKEN_FORMAT = re.compile(r"[0-9]+:[A-Za-z0-9_-]{20,}")
_SQLITE_MAGIC = b"SQLite format 3\x00"
_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
_UI = None
_known_secrets: list[str] | None = None


# --------------------------------------------------------------------------- helpers
_VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$")
_DEV_RE = re.compile(r"^dev-([0-9A-Fa-f]{7,40})$")
_SHA_RE = re.compile(r"^[0-9A-Fa-f]{7,40}$")


def resolve_installed_version(home: Path | None = None) -> str:
    """Read the installed release marker; VERSION is the runtime source of truth."""
    base = Path(home) if home is not None else HOME
    marker = base / "VERSION"
    try:
        raw = marker.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise RuntimeError(f"VERSION file not found: {marker}") from None
    except (OSError, UnicodeDecodeError):
        raise RuntimeError(f"VERSION file is unreadable: {marker}") from None
    if not raw:
        raise RuntimeError(f"VERSION file is empty: {marker}")
    if _VERSION_RE.fullmatch(raw):
        return raw.removeprefix("v")
    if _DEV_RE.fullmatch(raw):
        return raw.lower()
    if _SHA_RE.fullmatch(raw):
        return raw.lower()
    raise RuntimeError(f"Malformed VERSION value in {marker}")


def _version() -> str:
    try:
        return resolve_installed_version()
    except RuntimeError:
        return "unknown"


def _version_error() -> str | None:
    try:
        resolve_installed_version()
    except RuntimeError as exc:
        return str(exc)
    return None


def _github_json(path: str, timeout: float = 2.5):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ZIKO-CLI-Update-Checker",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def update_status(force: bool = False) -> dict[str, str | bool]:
    """Return cached GitHub stable/dev update metadata; never blocks the menu for long."""
    global _UPDATE_STATUS_CACHE
    now = time.monotonic()
    if not force and _UPDATE_STATUS_CACHE and now - _UPDATE_STATUS_CACHE[0] < 300:
        return dict(_UPDATE_STATUS_CACHE[1])
    current = _version()
    result: dict[str, str | bool] = {
        "current": current, "stable": "", "beta": "",
        "available": False, "reachable": False,
    }
    try:
        release = _github_json("releases/latest")
        tag = str(release.get("tag_name", ""))
        if not re.fullmatch(r"v\d+\.\d+\.\d+", tag) or release.get("draft"):
            raise ValueError("GitHub latest release has an invalid stable tag")
        commit = _github_json("commits/main")
        sha = str(commit.get("sha", ""))
        if not _SHA_RE.fullmatch(sha):
            raise ValueError("GitHub main branch returned an invalid commit SHA")
        stable = tag.removeprefix("v")
        beta = f"dev-{sha[:12].lower()}"
        result.update({
            "stable": stable, "beta": beta, "reachable": True,
            "available": current != stable,
        })
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, urllib.error.URLError):
        result["reachable"] = False
    _UPDATE_STATUS_CACHE = (now, result)
    return dict(result)


def available_stable_releases(limit: int = 20) -> list[tuple[str, str]]:
    releases = _github_json(f"releases?per_page={max(1, min(limit, 100))}")
    rows: list[tuple[str, str]] = []
    for release in releases:
        tag = str(release.get("tag_name", ""))
        if release.get("draft") or release.get("prerelease"):
            continue
        if re.fullmatch(r"v\d+\.\d+\.\d+", tag):
            rows.append((tag, str(release.get("name") or tag)))
    return rows


def _ui():
    """Import the Rich layer lazily so ``--help`` works even without Rich."""
    global _UI
    if _UI is None:
        try:
            from app import cli_menu
        except ModuleNotFoundError as exc:
            if (exc.name or "").split(".")[0] != "rich":
                raise
            print("Rich is not installed. Run: "
                  f"{HOME}/.venv/bin/pip install 'rich>=13.7'", file=sys.stderr)
            raise SystemExit(1) from None
        _UI = cli_menu
    return _UI


def _env_path() -> Path:
    return Path(os.environ.get("ZIKO_ENV_FILE", "/etc/ziko-assistant/ziko.env"))


def _scripts_dir() -> Path:
    return HOME / "scripts"


def _read_env() -> tuple[dict[str, str] | None, str | None]:
    """Return (values, problem). problem is None, 'missing' or 'denied'."""
    path = _env_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except (PermissionError, OSError, UnicodeDecodeError):
        return None, "denied"
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values, None


def redact(text: str) -> str:
    """Remove anything that looks like (or is) the bot token."""
    global _known_secrets
    if _known_secrets is None:
        values, _ = _read_env()
        token = (values or {}).get("BOT_TOKEN", "")
        _known_secrets = [token] if len(token) >= 8 else []
    for secret in _known_secrets:
        text = text.replace(secret, "[REDACTED]")
    return _TOKEN_RE.sub("[REDACTED]", text)


def _privileged(cmd: list[str]) -> list[str]:
    if os.geteuid() == 0 or not shutil.which("sudo"):
        return cmd
    return ["sudo", *cmd]


def _as_service_user(cmd: list[str]) -> list[str]:
    import pwd

    if pwd.getpwuid(os.geteuid()).pw_name == SERVICE_USER:
        return cmd
    if os.geteuid() == 0:
        return ["runuser", "-u", SERVICE_USER, "--", *cmd]
    return ["sudo", "-u", SERVICE_USER, *cmd] if shutil.which("sudo") else cmd


def _capture(cmd: list[str], timeout: float = 15) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                              timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    return proc.returncode, redact((proc.stdout or "") + (proc.stderr or "")).strip()


def _stream(cmd: list[str], line_filter: Callable[[str], bool] | None = None) -> int:
    """Run a command and print its output line by line with secrets redacted."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", bufsize=1)
    except FileNotFoundError:
        _ui().error(f"Command not found: {cmd[0]}")
        return 127
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            text = redact(line)
            if line_filter is not None and not line_filter(text):
                continue
            sys.stdout.write(text)
            sys.stdout.flush()
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        return 130


def _inherit(cmd: list[str]) -> int:
    try:
        return subprocess.run(cmd, check=False).returncode
    except FileNotFoundError:
        _ui().error(f"Command not found: {cmd[0]}")
        return 127


def _script(name: str) -> Path | None:
    path = _scripts_dir() / name
    if path.is_file():
        return path
    _ui().error(f"Script not found: {path}")
    return None


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _human_duration(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    parts = [f"{d} days" if d else "", f"{h} hours" if h else "", f"{m} minutes" if m and not d else ""]
    parts = [p for p in parts if p]
    return ", ".join(parts[:2]) or "Less than a minute"


def _cpu_counters() -> tuple[int, int] | None:
    """Return aggregate CPU total and idle counters from procfs."""
    try:
        fields = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()
        if fields[0] != "cpu":
            return None
        values = [int(x) for x in fields[1:]]
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return total, idle
    except (OSError, ValueError, IndexError):
        return None


def _system_cpu_percent(sample_seconds: float = 0.12) -> float | None:
    first = _cpu_counters()
    if first is None:
        return None
    time.sleep(sample_seconds)
    second = _cpu_counters()
    if second is None:
        return None
    total_delta = second[0] - first[0]
    idle_delta = second[1] - first[1]
    if total_delta <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (total_delta - idle_delta) / total_delta))


def _system_resources() -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    cpu = _system_cpu_percent()
    rows.append(("CPU usage", f"{cpu:.1f}%" if cpu is not None else "Unavailable"))
    try:
        mem = {}
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, _, value = line.partition(":")
            parts = value.split()
            if parts and parts[0].isdigit():
                mem[key] = int(parts[0]) * 1024
        total = mem["MemTotal"]
        available = mem.get("MemAvailable", mem.get("MemFree", 0))
        used = max(0, total - available)
        pct = (used / total * 100) if total else 0
        rows.append(("RAM", f"{_human_size(used)} / {_human_size(total)} ({pct:.1f}%)"))
    except (OSError, KeyError, ValueError):
        rows.append(("RAM", "Unavailable"))
    try:
        disk = shutil.disk_usage(HOME if HOME.exists() else "/")
        used = disk.total - disk.free
        rows.append(("Disk", f"{_human_size(used)} used / {_human_size(disk.free)} free"))
    except OSError:
        rows.append(("Disk", "Unavailable"))
    try:
        load = Path("/proc/loadavg").read_text(encoding="ascii").split()[:3]
        rows.append(("Load average", " / ".join(load)))
    except (OSError, IndexError):
        rows.append(("Load average", "Unavailable"))
    try:
        uptime = float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
        rows.append(("System uptime", _human_duration(uptime)))
    except (OSError, ValueError, IndexError):
        rows.append(("System uptime", "Unavailable"))
    return rows


def _process_usage(pid: int, sample_seconds: float = 0.12) -> tuple[float | None, int | None]:
    """Approximate main-process CPU percent (one core = 100%) and RSS bytes."""
    def snapshot() -> tuple[int, int] | None:
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            tail = raw[raw.rfind(")") + 2:].split()
            ticks = int(tail[11]) + int(tail[12])  # utime + stime
            status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
            rss_kb = next(int(line.split()[1]) for line in status.splitlines()
                          if line.startswith("VmRSS:"))
            return ticks, rss_kb * 1024
        except (OSError, ValueError, StopIteration, IndexError):
            return None
    first = snapshot()
    if first is None:
        return None, None
    time.sleep(sample_seconds)
    second = snapshot()
    if second is None:
        return None, first[1]
    elapsed_ticks = max(0, second[0] - first[0])
    cpu = elapsed_ticks / os.sysconf("SC_CLK_TCK") / sample_seconds * 100.0
    return cpu, second[1]


# ----------------------------------------------------------------------- systemd info
def service_info() -> dict[str, str]:
    rc, out = _capture(["systemctl", "show", SERVICE, "-p",
                        "LoadState,ActiveState,SubState,UnitFileState,MainPID,"
                        "ActiveEnterTimestampMonotonic,NRestarts,MemoryCurrent"])
    info: dict[str, str] = {}
    if rc == 0:
        for line in out.splitlines():
            key, _, value = line.partition("=")
            info[key] = value
    return info


def service_state(info: dict[str, str] | None = None) -> str:
    info = service_info() if info is None else info
    if not info:
        return "unknown"
    if info.get("LoadState") == "not-found":
        return "not-found"
    return info.get("ActiveState") or "unknown"


def _db_path(values: dict[str, str] | None) -> Path:
    raw = ((values or {}).get("DATABASE_PATH") or "/var/lib/ziko-assistant/ziko.sqlite3").strip()
    path = Path(raw).expanduser()
    return path if path.is_absolute() else HOME / path


def _last_backup() -> tuple[float | None, str | None]:
    """Return (mtime, problem). problem: None, 'none' or 'denied'."""
    try:
        root = Path(os.environ.get("ZIKO_BACKUP_DIR", "/var/backups/ziko-assistant"))
        # Since 1.4 backups are age-encrypted (ziko-*.sqlite3.age); plain ones are legacy.
        files = [*root.glob("ziko-*.sqlite3"), *root.glob("ziko-*.sqlite3.age")]
        times = [f.stat().st_mtime for f in files]
    except PermissionError:
        return None, "denied"
    except OSError:
        return None, "none"
    return (max(times), None) if times else (None, "none")


# ------------------------------------------------------------------------- commands
def cmd_status() -> int:
    ui = _ui()
    info = service_info()
    state = service_state(info)
    values, env_problem = _read_env()
    rows: list[tuple[str, object]] = [("Service", ui.indicator(state))]

    boot = info.get("UnitFileState", "")
    if boot:
        rows.append(("Enabled at boot", "Enabled" if boot == "enabled" else boot))
    mono = info.get("ActiveEnterTimestampMonotonic", "0")
    if state == "active" and mono.isdigit() and int(mono) > 0:
        rows.append(("Uptime", _human_duration(time.monotonic() - int(mono) / 1e6)))
    if info.get("MainPID", "0") not in {"", "0"}:
        rows.append(("PID", info["MainPID"]))
    mem = info.get("MemoryCurrent", "")
    if mem.isdigit() and int(mem) < 2**63:
        rows.append(("Memory", _human_size(int(mem))))
    if info.get("NRestarts", "0") not in {"", "0"}:
        rows.append(("Restart count", info["NRestarts"]))

    if env_problem == "missing":
        rows.append(("Configuration", ".env file not found"))
    elif env_problem == "denied":
        rows.append(("Configuration", "Permission denied (run with sudo)"))
    else:
        token = (values or {}).get("BOT_TOKEN", "")
        good = bool(_TOKEN_FORMAT.fullmatch(token))
        rows.append(("Bot token", "Configured" if good else "Not configured or invalid"))
    try:
        size = _db_path(values).stat().st_size
        rows.append(("Database", _human_size(size)))
    except FileNotFoundError:
        rows.append(("Database", "Not created yet"))
    except PermissionError:
        rows.append(("Database", "Permission denied"))
    except OSError:
        rows.append(("Database", "Unavailable (filesystem error)"))
    mtime, problem = _last_backup()
    if mtime:
        rows.append(("Last backup", f"{_human_duration(time.time() - mtime)} ago"))
    else:
        rows.append(("Last backup", "Permission denied" if problem == "denied" else "None"))
    version = _version()
    rows.append(("Version", version if version != "unknown" else "Unavailable"))
    if version == "unknown":
        rows.append(("Version source", _version_error() or "VERSION unavailable"))
    tenant_path = _db_path(values).with_name("tenants.sqlite3")
    try:
        registry_exists = tenant_path.is_file()
    except (PermissionError, OSError):
        registry_exists = None
    if registry_exists is None:
        rows.append(("Child bots", "Permission denied / registry unavailable"))
    elif registry_exists:
        try:
            with sqlite3.connect(f"file:{tenant_path}?mode=ro", uri=True, timeout=3) as tenant_db:
                total, enabled = tenant_db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(enabled), 0) FROM tenants"
                ).fetchone()
            rows.append(("Child bots", f"{enabled} enabled / {total} registered"))
        except (sqlite3.Error, PermissionError, OSError):
            rows.append(("Child bots", "Registry unavailable"))
    else:
        rows.append(("Child bots", "0 registered"))
    rows.append((" ", ""))
    rows.append(("SERVER RESOURCES", ""))
    rows.extend(_system_resources())
    pid_text = info.get("MainPID", "0")
    if pid_text.isdigit() and int(pid_text) > 0:
        proc_cpu, proc_mem = _process_usage(int(pid_text))
        rows.append(("ZIKO process CPU", f"{proc_cpu:.1f}%" if proc_cpu is not None else "Unavailable"))
        rows.append(("ZIKO process RAM", _human_size(proc_mem) if proc_mem is not None else "Unavailable"))
    ui.render_status(rows)  # type: ignore[arg-type]
    return 0 if state == "active" else 3


def _wait_for(target: str, timeout: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if service_state() == target:
            if target != "active":
                return True
            time.sleep(1.5)  # guard against an immediate crash loop
            return service_state() == "active"
        time.sleep(0.5)
    return False


def _systemctl_action(action: str) -> int:
    ui = _ui()
    if not shutil.which("systemctl"):
        ui.error("systemctl is not available on this system.")
        return 1
    labels = {"start": "Start", "stop": "Stop", "restart": "Restart"}
    state = service_state()
    if state == "not-found":
        ui.error("The ziko service is not installed.")
        return 1
    if action == "start" and state == "active":
        ui.info("The service is already active.")
        return 0
    if action == "stop" and state in {"inactive", "failed"}:
        ui.info("The service is already stopped.")
        return 0
    rc = _inherit(_privileged(["systemctl", action, SERVICE]))
    if rc != 0:
        ui.error(f"Failed to {action} the service.")
        return rc
    target = "inactive" if action == "stop" else "active"
    with ui.console.status(f"{labels[action]} service..."):
        reached = _wait_for(target)
    if reached:
        ui.ok(f"Service {'stopped' if action == 'stop' else 'is active'}.")
        return 0
    ui.error("The service did not reach the expected state. Check: ziko logs")
    return 1


def cmd_start() -> int:
    return _systemctl_action("start")


def cmd_stop() -> int:
    return _systemctl_action("stop")


def cmd_restart() -> int:
    return _systemctl_action("restart")


def _can_read_journal() -> bool:
    if os.geteuid() == 0:
        return True
    import grp

    names = set()
    for gid in os.getgroups():
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            pass
    return bool(names & {"adm", "systemd-journal"})


_LOG_LEVELS = ("WARNING", "ERROR", "CRITICAL")


def _log_directory(values: dict[str, str] | None) -> Path:
    """Same rule as the app: ZIKO_LOG_DIR if set, otherwise <database dir>/logs."""
    raw = ((values or {}).get("ZIKO_LOG_DIR") or "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else HOME / path
    return _db_path(values).parent / "logs"


def _level_filter(level: str | None) -> Callable[[str], bool] | None:
    """Keep lines at ``level`` or above (WARNING < ERROR < CRITICAL)."""
    if level is None:
        return None
    wanted = _LOG_LEVELS[_LOG_LEVELS.index(level.upper()):]
    pattern = re.compile(r" (" + "|".join(wanted) + r") ")
    return lambda line: bool(pattern.search(line))


def cmd_file_logs(
    *, bot_id: int | None = None, lines: int = 100, follow: bool = False, level: str | None = None,
) -> int:
    """Read ZIKO's own log files: master.log, or tenants/bot-<id>.log with --bot."""
    values, _ = _read_env()
    directory = _log_directory(values)
    if bot_id is None:
        path = directory / "master.log"
    else:
        path = directory / "tenants" / f"bot-{int(bot_id)}.log"
    cmd = ["tail", "-n", str(max(1, lines)), str(path)]
    if follow:
        cmd.insert(1, "-F")
    if not os.access(path.parent if not path.exists() else path, os.R_OK):
        cmd = _as_service_user(cmd)
    line_filter = _level_filter(level)
    if follow:
        rc = _stream(cmd, line_filter)
        return 0 if rc == 130 else rc
    rc, output = _capture(cmd)
    if rc != 0:
        _ui().error(f"Cannot read {path}: {output or 'no output'}")
        return rc
    for line in output.splitlines():
        if line_filter is None or line_filter(line):
            print(line)
    return 0


def cmd_logs(lines: int = 100, follow: bool = False) -> int:
    cmd = ["journalctl", "-u", SERVICE, "-n", str(max(1, lines)), "--no-pager", "-o", "short-iso"]
    if follow:
        cmd.append("-f")
    if not _can_read_journal():
        cmd = _privileged(cmd)
    rc = _stream(cmd)
    return 0 if rc == 130 else rc


_AGE_RECIPIENT = re.compile(r"age1[02-9ac-hj-np-z]{58}")
_AGE_IDENTITY_DEFAULT = "/etc/ziko-assistant/backup-age-identity"


def _age_recipient(values: dict[str, str] | None = None) -> tuple[str | None, str]:
    """Backup recipient (an age PUBLIC key) and where it came from, same order as update.sh:
    the environment, then the config file, then derived from the root-only identity.

    Returns (None, reason) when none is usable; the identity's secret is never read here,
    only ``age-keygen -y`` (which prints the public key) touches it.
    """
    value = os.environ.get("ZIKO_AGE_RECIPIENT", "").strip()
    origin = "environment"
    if not value:
        value = ((values or {}).get("ZIKO_AGE_RECIPIENT") or "").strip().strip("\"'")
        origin = "config"
    if not value:
        identity = Path(os.environ.get("ZIKO_AGE_IDENTITY") or _AGE_IDENTITY_DEFAULT)
        try:
            st = identity.lstat()
        except OSError:
            return None, f"no backup key and no identity at {identity}"
        import stat as _stat
        if not _stat.S_ISREG(st.st_mode) or st.st_uid != 0 or _stat.S_IMODE(st.st_mode) not in (0o400, 0o600):
            return None, f"{identity} must be a regular root-owned file with mode 0400 or 0600"
        try:
            out = subprocess.run(["age-keygen", "-y", str(identity)], capture_output=True, text=True,
                                 timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None, "age-keygen is not installed (package: age)"
        lines = out.stdout.split()
        value, origin = (lines[0] if out.returncode == 0 and lines else ""), "derived"
        if not value:
            return None, f"age-keygen could not read {identity}"
    if not _AGE_RECIPIENT.fullmatch(value):
        return None, f"ZIKO_AGE_RECIPIENT ({origin}) is not an age public key (age1...)"
    return value, origin


def cmd_backup() -> int:
    ui = _ui()
    script = _script("backup.sh")
    if not script:
        return 1
    values, _problem = _read_env()
    recipient, origin = _age_recipient(values)
    if recipient is None:
        ui.error(f"Backup not started: {origin}.")
        return 1
    if origin == "derived":
        ui.info(f"Backup key derived from the server identity: {recipient[:12]}...")
    env = {**os.environ, "ZIKO_AGE_RECIPIENT": recipient}
    forwarded_env = [
        f"{name}={env[name]}"
        for name in ("ZIKO_AGE_RECIPIENT", "ZIKO_AGE_IDENTITY")
        if env.get(name)
    ]
    rc = _stream(_as_service_user(["env", *forwarded_env, "bash", str(script)]))
    if rc == 0:
        ui.ok("Backup completed.")
    else:
        ui.error("Backup failed.")
    return rc


def cmd_restore(file: str, yes: bool = False) -> int:
    ui = _ui()
    path = Path(file).expanduser().absolute()
    if not path.is_file():
        ui.error(f"Backup file not found: {path}")
        return 2
    platform_archive = path.name.endswith(".tar.gz") or path.name.endswith(".tar.gz.age") or path.suffix == ".tgz"
    encrypted_database = path.name.endswith(".sqlite3.age")
    if platform_archive:
        script = _script("restore-platform.sh")
        if not script:
            return 1
        ui.warn(
            "A full platform restore replaces the main database, child-bot registry, "
            "encryption key, and child-bot databases. A verified pre-restore snapshot "
            "will be created first."
        )
        if not yes and not ui.confirm(f"Restore full platform backup {path.name}?"):
            ui.info("Cancelled.")
            return 1
        command = ["bash", str(script), *( ["--yes"] if yes else []), str(path)]
        rc = _stream(_privileged(command))
        if rc != 0:
            ui.error("Platform restore failed.")
            return rc
        ui.ok("Full platform restore completed. The original service state was preserved.")
        return 0
    try:
        if not encrypted_database:
            with path.open("rb") as fh:
                if fh.read(16) != _SQLITE_MAGIC:
                    ui.error("This file is not a valid SQLite database or platform archive.")
                    return 2
    except PermissionError:
        ui.warn("Cannot inspect the file (permission denied); the restore script will validate it.")
    script = _script("restore.sh")
    if not script:
        return 1
    ui.warn("The current database will be replaced (a .pre-restore copy will be kept) "
            "and the service will be stopped briefly.")
    if not yes and not ui.confirm(f"Restore from {path.name}?"):
        ui.info("Cancelled.")
        return 1
    rc = _stream(_privileged(["bash", str(script), *( ["--yes"] if yes else []), str(path)]))
    if rc != 0:
        ui.error("Restore failed.")
        return rc
    ui.ok("Restore completed.")
    return 0 if _wait_for("active") else 1


def cmd_update(
    yes: bool = False,
    repo: str | None = None,
    version: str | None = None,
    channel: str = "stable",
    check: bool = False,
    allow_unverified: bool = False,
) -> int:
    ui = _ui()
    script = _script("update.sh")
    if not script:
        return 1
    if channel == "dev":
        channel = "beta"
    if channel not in {"stable", "beta"}:
        ui.error("Update channel must be stable, beta or dev.")
        return 2
    command = ["bash", str(script), "--channel", channel, "--repo", repo or GITHUB_REPO]
    if version:
        command += ["--version", version]
    if channel == "beta" and not check:
        if not allow_unverified:
            ui.warn("Beta builds are NOT verified: no release checksum and no build attestation.")
            if not sys.stdin.isatty() or ui.ask("Type UNVERIFIED to install anyway") != "UNVERIFIED":
                ui.info("Cancelled. Nothing was changed.")
                return 1
        command.append("--allow-unverified")
    if check:
        command.append("--check")
        return _inherit(_privileged(command))
    # Update Center is intentionally one-choice-to-install: no second confirmation prompt.
    rc = _inherit(_privileged(command))
    if rc != 0:
        ui.error("Update failed. The updater should have rolled back automatically; inspect service logs.")
        return rc
    ui.ok("Update completed.")
    global _UPDATE_STATUS_CACHE
    _UPDATE_STATUS_CACHE = None
    return cmd_status()


def cmd_uninstall(yes: bool = False) -> int:
    ui = _ui()
    script = _script("uninstall.sh")
    if not script:
        return 1
    ui.warn("Uninstalling ZIKO will stop the service and remove data according to the uninstall script.")
    if not yes:
        if ui.confirm("Create a backup before uninstalling?", default=True) and cmd_backup() != 0:
            ui.error("Uninstall cancelled because backup failed.")
            return 1
        if ui.ask("Type uninstall to confirm") != "uninstall":
            ui.info("Cancelled.")
            return 1
    return _inherit(_privileged(["bash", str(script), *(["--yes"] if yes else [])]))


def _config_rows(values: dict[str, str]) -> list[tuple[str, object]]:
    token = values.get("BOT_TOKEN", "")
    if not token:
        token_cell = "Not configured"
    else:
        good = bool(_TOKEN_FORMAT.fullmatch(token))
        token_cell = "•••••••• (configured" + (", valid format)" if good else ", invalid format!)")
    ids = [x for x in re.split(r"[,;\s]+", values.get("ADMIN_IDS", "")) if x]
    rows: list[tuple[str, object]] = [
        ("BOT_TOKEN", token_cell),
        ("ADMIN_IDS", f"{len(ids)} admin(s)" if ids else "Not configured"),
        ("DATABASE_PATH", values.get("DATABASE_PATH") or "/var/lib/ziko-assistant/ziko.sqlite3 (default)"),
        ("LOG_LEVEL", values.get("LOG_LEVEL") or "INFO (default)"),
    ]
    for key in values:
        if key not in {"BOT_TOKEN", "ADMIN_IDS", "DATABASE_PATH", "LOG_LEVEL"}:
            rows.append((key, "Configured (value hidden)"))
    return rows


def cmd_config(edit: bool = False) -> int:
    ui = _ui()
    if edit:
        if os.geteuid() == 0:
            editor = os.environ.get("EDITOR") or ("nano" if shutil.which("nano") else "vi")
            rc = _inherit([editor, str(_env_path())])
        else:
            rc = _inherit(["sudoedit", str(_env_path())])
        if rc == 0:
            ui.ok("Saved. Apply changes with: ziko restart")
        return rc
    values, problem = _read_env()
    if problem == "missing":
        ui.error(f"Configuration file not found: {_env_path()}")
        return 1
    if problem == "denied":
        ui.error("Cannot read .env. Run with sudo: sudo ziko config")
        return 1
    ui.render_config(str(_env_path()), _config_rows(values or {}))  # type: ignore[arg-type]
    try:
        mode = _env_path().stat().st_mode & 0o777
        if mode & 0o027:
            ui.warn(f"Configuration permissions are {mode:o}; expected root:ziko with mode 640.")
    except OSError:
        pass
    ui.info("To edit: ziko config --edit (the token is never displayed)")
    return 0


def _telegram_api(token: str, method: str, payload: dict[str, object] | None = None,
                  timeout: float = 7.0) -> tuple[bool, dict[str, object] | str]:
    """Call Telegram Bot API without ever returning a URL or token in errors."""
    from urllib.parse import urlencode

    if not _TOKEN_FORMAT.fullmatch(token):
        return False, "Token format is invalid"
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urlencode(payload or {}).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        if isinstance(body, dict) and body.get("ok") is True:
            result = body.get("result", {})
            return True, result if isinstance(result, dict) else {}
        description = body.get("description", "Telegram API returned an error") if isinstance(body, dict) else "Invalid API response"
        return False, redact(str(description)[:240])
    except urllib.error.HTTPError as exc:
        # Do not expose the exception: its URL includes the bot token.
        try:
            body = json.loads(exc.read().decode("utf-8"))
            message = str(body.get("description", "HTTP error")) if isinstance(body, dict) else "HTTP error"
        except (ValueError, OSError):
            message = f"HTTP {exc.code}"
        return False, redact(message[:240])
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return False, "Network, TLS, timeout, or invalid JSON error"


def _telegram_diagnostic_checks(values: dict[str, str] | None) -> list[tuple[str, str, str]]:
    checks: list[tuple[str, str, str]] = []
    token = (values or {}).get("BOT_TOKEN", "")
    if not _TOKEN_FORMAT.fullmatch(token):
        checks.append(("fail", "Telegram getMe", "Skipped: token is missing or malformed"))
        return checks
    ok, result = _telegram_api(token, "getMe")
    if not ok:
        checks.append(("fail", "Telegram getMe", str(result)))
        return checks
    username = str(result.get("username", "bot")) if isinstance(result, dict) else "bot"
    checks.append(("ok", "Telegram getMe", f"Token valid; @{username}"))
    hook_ok, hook = _telegram_api(token, "getWebhookInfo")
    if not hook_ok:
        checks.append(("warn", "Webhook / polling", f"getWebhookInfo failed: {hook}"))
    else:
        webhook_url = str(hook.get("url", "")) if isinstance(hook, dict) else ""
        if webhook_url:
            pending = hook.get("pending_update_count", "?")
            last_error = str(hook.get("last_error_message", "") or "")
            checks.append(("ok", "Webhook / polling", f"Webhook configured; pending updates: {pending}"))
            if last_error:
                checks.append(("warn", "Webhook delivery", last_error[:220]))
        else:
            checks.append(("ok", "Webhook / polling", "No webhook configured; polling mode expected"))
    return checks


def _poller_diagnostics() -> list[tuple[str, str, str]]:
    checks: list[tuple[str, str, str]] = []
    matches: list[int] = []
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\\x00", b" ").decode("utf-8", "replace")
            except OSError:
                continue
            if "app.main" in cmdline and int(entry.name) != os.getpid():
                matches.append(int(entry.name))
    except OSError:
        checks.append(("warn", "Polling processes", "Could not inspect /proc"))
        return checks
    # The systemd main process is expected; multiple app.main processes can indicate duplicate pollers.
    if len(matches) > 1:
        checks.append(("warn", "Polling processes", f"{len(matches)} app.main processes detected; inspect duplicate pollers"))
    elif matches:
        checks.append(("ok", "Polling processes", "One application process detected"))
    else:
        checks.append(("warn", "Polling processes", "No app.main process found"))
    if shutil.which("journalctl"):
        rc, logs = _capture(["journalctl", "-u", SERVICE, "-n", "250", "--no-pager"])
        if rc == 0:
            if "409 Conflict" in logs or "terminated by other getUpdates request" in logs:
                checks.append(("fail", "Telegram polling conflict", "409 Conflict detected; another poller may be using this token"))
            else:
                checks.append(("ok", "Telegram polling conflict", "No recent 409 Conflict in service logs"))
            if re.search(r"update_id|update received|processing update", logs, re.I):
                checks.append(("ok", "Update processing logs", "Recent update activity found"))
            else:
                checks.append(("warn", "Update processing logs", "No recognizable recent update activity; send a test message to the bot"))
    return checks


def run_checks() -> list[tuple[str, str, str]]:
    checks: list[tuple[str, str, str]] = []

    def add(level: str, title: str, detail: str) -> None:
        checks.append((level, title, detail))

    v = sys.version_info
    add("ok" if v[:2] == (3, 12) else "fail", "Python version",
        f"{v.major}.{v.minor}.{v.micro}" + ("" if v[:2] == (3, 12) else " (Python 3.12 required)"))
    add("ok" if HOME.is_dir() else "fail", "Application directory", str(HOME))
    try:
        installed = resolve_installed_version()
        add("ok", "Installed version marker", installed)
    except RuntimeError as exc:
        add("fail", "Installed version marker", str(exc))
    venv_py = HOME / ".venv" / "bin" / "python"
    add("ok" if os.access(venv_py, os.X_OK) else "fail", "Virtual environment (venv)", str(venv_py))

    values, problem = _read_env()
    if problem == "missing":
        add("fail", ".env file", "Not found")
    elif problem == "denied":
        add("warn", ".env file", "Read permission denied; run with sudo")
    else:
        try:
            mode = _env_path().stat().st_mode & 0o777
            add("ok" if not mode & 0o027 else "warn", "Configuration permissions",
                f"{mode:o}" + ("" if not mode & 0o027 else " (expected root:ziko with mode 640)"))
        except OSError:
            add("warn", "Configuration permissions", "Could not inspect file metadata")
        values = values or {}
        token = values.get("BOT_TOKEN", "")
        if not token or token.startswith("000000000:REPLACE"):
            add("fail", "Bot token", "Not configured or still using the sample value")
        elif not _TOKEN_FORMAT.fullmatch(token):
            add("fail", "Bot token", "Invalid format")
        else:
            add("ok", "Bot token", "Configured (hidden)")
        ids = [x for x in re.split(r"[,;\s]+", values.get("ADMIN_IDS", "")) if x]
        good_ids = bool(ids) and all(x.isascii() and x.isdigit() and int(x) > 0 for x in ids)
        add("ok" if good_ids else "fail", "ADMIN_IDS",
            f"{len(ids)} admin(s)" if good_ids else "At least one valid numeric ID is required")
        level = (values.get("LOG_LEVEL") or "INFO").upper()
        add("ok" if level in _LOG_LEVELS else "fail", "LOG_LEVEL", level)

    db = _db_path(values)
    try:
        with db.open("rb") as fh:
            header_ok = fh.read(16) == _SQLITE_MAGIC
        mode = db.stat().st_mode & 0o777
        if not header_ok:
            add("fail", "Database", "Invalid SQLite header")
        elif mode & 0o077:
            add("warn", "Database", f"Permissions are {mode:o}; expected 600")
        else:
            add("ok", "Database", _human_size(db.stat().st_size))
    except FileNotFoundError:
        add("warn", "Database", "Not created yet (created on first run)")
    except PermissionError:
        add("warn", "Database", "Permission denied; run with sudo")
    except OSError:
        add("warn", "Database", "Unavailable (filesystem error)")

    if not shutil.which("systemctl"):
        add("fail", "systemd", "systemctl not found")
    else:
        info = service_info()
        state = service_state(info)
        if state == "not-found":
            add("fail", "ZIKO service", "Not installed")
        else:
            add("ok" if state == "active" else "fail", "ZIKO service", state)
            enabled = info.get("UnitFileState") == "enabled"
            add("ok" if enabled else "warn", "Enabled at boot", info.get("UnitFileState", "?"))
            if info.get("NRestarts", "0") not in {"", "0"}:
                add("warn", "Restarts", f"{info['NRestarts']} time(s) since last start")

    for name, level in (("backup.sh", "fail"), ("restore.sh", "fail"),
                        ("restore-platform.sh", "warn"), ("update.sh", "warn"),
                        ("uninstall.sh", "warn")):
        try:
            found = (_scripts_dir() / name).is_file()
        except OSError:
            found = False
        add("ok" if found else level, f"Script {name}", "Present" if found else "Not found / inaccessible")

    try:
        socket.create_connection(("api.telegram.org", 443), timeout=5).close()
        add("ok", "Telegram connectivity", "api.telegram.org:443")
    except OSError:
        add("warn", "Telegram connectivity", "Connection failed (check network/firewall)")

    checks.extend(_telegram_diagnostic_checks(values))
    checks.extend(_poller_diagnostics())

    try:
        free = shutil.disk_usage(HOME if HOME.is_dir() else "/").free
        level = "fail" if free < 100 * 2**20 else "warn" if free < 500 * 2**20 else "ok"
        add(level, "Disk space", f"{_human_size(free)} free")
    except OSError:
        add("warn", "Disk space", "Unknown")

    mtime, problem = _last_backup()
    if mtime is None:
        add("warn", "Backup", "Permission denied" if problem == "denied" else "No backup has been created yet")
    else:
        age = time.time() - mtime
        add("ok" if age < 7 * 86400 else "warn", "Backup", f"{_human_duration(age)} ago")
    return checks


def cmd_bots_list() -> int:
    """List registered child bots without reading or decrypting their tokens."""
    ui = _ui()
    values, problem = _read_env()
    if problem is not None:
        ui.error("Cannot read configuration; run with sudo.")
        return 1
    registry_path = _db_path(values).with_name("tenants.sqlite3")
    try:
        registry_exists = registry_path.is_file()
    except (PermissionError, OSError):
        ui.error("Cannot inspect the child-bot registry (permission or filesystem error).")
        return 1
    if not registry_exists:
        ui.info("No child-bot registry exists yet.")
        return 0
    try:
        connection = sqlite3.connect(f"file:{registry_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = connection.execute(
                "SELECT user_id, bot_id, username, enabled, created_at "
                "FROM tenants ORDER BY created_at, bot_id"
            ).fetchall()
        finally:
            connection.close()
    except (sqlite3.Error, OSError):
        ui.error("Could not read the child-bot registry.")
        return 1
    ui.render_bot_list(rows)
    ui.info("Create bots with /create_bot in the master Telegram bot.")
    ui.info("Platform administrators: /tenants, /tenant_enable BOT_ID, /tenant_disable BOT_ID.")
    return 0


def cmd_doctor(send_test: bool = False) -> int:
    ui = _ui()
    with ui.console.status("Running diagnostics..."):
        checks = run_checks()
    ui.render_checks(checks)
    if send_test:
        values, problem = _read_env()
        admins = [x for x in re.split(r"[,;\\s]+", (values or {}).get("ADMIN_IDS", "")) if x]
        token = (values or {}).get("BOT_TOKEN", "")
        if problem or not admins or not all(x.isascii() and x.isdigit() and int(x) > 0 for x in admins):
            ui.error("Cannot send test message: valid BOT_TOKEN and ADMIN_IDS are required.")
            return 1
        target = admins[0]
        if not ui.confirm(f"Send a test message to configured administrator ID {target}?"):
            ui.info("Test message cancelled.")
            return 0
        ok, result = _telegram_api(token, "sendMessage", {
            "chat_id": target,
            "text": "ZIKO diagnostic test message. Telegram API and message delivery are working.",
        })
        if ok:
            ui.ok(f"Test message sent to configured administrator ID {target}.")
        else:
            ui.error(f"Test message failed: {result}")
            return 1
    return 1 if any(c[0] == "fail" for c in checks) else 0


def cmd_version() -> int:
    try:
        ui = _ui()
    except SystemExit:
        print(f"ZIKO {_version()}")
        return 0
    current = _version()
    ui.print_logo(current, version_error=_version_error())
    ui.console.print(f"Python {sys.version.split()[0]}  ·  {HOME}")
    return 0 if current != "unknown" else 1


def cmd_help() -> int:
    ui = _ui()
    ui.render_help_about(
        version=_version(),
        version_error=_version_error(),
        commands=[
            ("status", "Show service health and installation details"),
            ("bots list", "List registered child bots without exposing tokens"),
            ("start / stop / restart", "Control the main ZIKO service"),
            ("logs", "View service logs with token redaction"),
            ("backup / restore", "Create or restore a verified platform backup"),
            ("update", "Check or install Stable / Dev releases"),
            ("config", "Inspect or safely edit configuration"),
            ("doctor", "Run installation diagnostics"),
            ("version", "Show the installed release marker"),
            ("uninstall", "Remove ZIKO after explicit confirmation"),
        ],
    )
    return 0


# ---------------------------------------------------------------------------- parser
def _update_dispatch(args: argparse.Namespace) -> int:
    from app import cli_updates
    action = args.action
    if action is None and (args.check or args.version or args.channel != "stable"):
        action = "legacy"  # pre-1.5 flags: ziko update --check / --version / --channel
    try:
        installed = resolve_installed_version()
    except RuntimeError as exc:
        print(f"Cannot read the installed version: {exc}", file=sys.stderr)
        return 1
    repo = args.repo or GITHUB_REPO
    values, _problem = _read_env()
    paths = cli_updates.default_paths(_db_path(values))
    if action == "legacy":
        return cmd_update(args.yes, args.repo, args.version, args.channel, args.check, args.allow_unverified)
    if action is None:
        return cli_updates.cmd_interactive(
            installed=installed, repo=repo, yes=args.yes,
            run_update=lambda channel: cmd_update(True, args.repo, None, channel, False, False))
    if action == "check":
        return cli_updates.cmd_check(installed=installed, repo=repo, as_json=args.json)
    if action == "stable":
        return cmd_update(args.yes, args.repo, args.version, "stable", False, False)
    if action == "beta":
        return cmd_update(args.yes, args.repo, None, "beta", False, args.allow_unverified)
    if action == "history":
        return cli_updates.cmd_history(data_dir=paths["data_dir"], as_json=args.json)
    if action == "logs":
        return cli_updates.cmd_logs(data_dir=paths["data_dir"], run_id=args.run_id, as_json=args.json)
    if action == "verify":
        return cli_updates.cmd_verify(root=HOME, record=args.record, as_json=args.json)
    if action == "rollback":
        return cli_updates.cmd_rollback(root=HOME, backup_dir=paths["backup_dir"], installed=installed,
                                        target=args.to, with_data=args.with_data, yes=args.yes)
    return 2


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ziko",
        description="ZIKO - Telegram assistant manager. Run without a command to open the interactive menu.",
    )
    p.add_argument("-V", "--version", action="store_true", help="Show version")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("status", help="Service status and overall health")
    sub.add_parser("start", help="Start the service")
    sub.add_parser("stop", help="Stop the service")
    sub.add_parser("restart", help="Restart the service")
    logs = sub.add_parser("logs", help="View logs (tokens are redacted)")
    logs.add_argument("-n", "--lines", type=int, default=100, help="Number of lines (default: 100)")
    logs.add_argument("-f", "--follow", action="store_true", help="Follow live logs")
    logs.add_argument("--master", action="store_true", help="Read master.log instead of the journal")
    logs.add_argument("--bot", type=int, metavar="BOT_ID", help="Read one child bot's log file")
    logs.add_argument("--level", choices=["WARNING", "ERROR", "CRITICAL"],
                      help="With --master or --bot: only lines at this level or above")
    upd = sub.add_parser("update", help="Check, install, verify and roll back releases",
                         description="Without ACTION: check for a stable release, show its notes and "
                                     "install it after confirmation.")
    upd.add_argument("action", nargs="?", choices=("check", "stable", "beta", "history", "rollback",
                                                   "verify", "logs"),
                     help="check | stable | beta | history | rollback | verify | logs")
    upd.add_argument("run_id", nargs="?", help="with 'logs': a run id from 'ziko update history'")
    upd.add_argument("--json", action="store_true", help="Machine-readable output (check, history, verify, logs)")
    upd.add_argument("--to", metavar="VERSION", help="with 'rollback': a specific earlier version")
    upd.add_argument("--with-data", action="store_true",
                     help="with 'rollback': also restore databases from the matching backup (newer data is lost)")
    upd.add_argument("--record", action="store_true",
                     help="with 'verify': record a manifest for an install made before v1.5.0")
    upd.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    upd.add_argument("--repo", help="GitHub repository in OWNER/REPOSITORY format")
    upd.add_argument("--version", help="Release tag, e.g. vX.Y.Z")
    upd.add_argument("--channel", choices=("stable", "beta", "dev"), default="stable",
                     help="Stable release or latest main-branch beta")
    upd.add_argument("--check", action="store_true", help="Check the selected channel without updating")
    upd.add_argument("--allow-unverified", action="store_true",
                     help="Required for beta: accept code with no checksum or build attestation")
    sec = sub.add_parser("security", help="Security audit and key management")
    sec_sub = sec.add_subparsers(dest="security_command", metavar="ACTION")
    sa = sec_sub.add_parser("audit", help="Read-only audit: permissions, hardening, keys, backups, leaked tokens")
    sa.add_argument("--json", action="store_true", help="Machine-readable output")
    rk = sec_sub.add_parser("rotate-key", help="Re-encrypt all child-bot tokens with a new key (service must be stopped)")
    rk.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    mk = sec_sub.add_parser("move-key", help="Move the token key to /etc/ziko-assistant (out of data and backups)")
    mk.add_argument("--to-data", action="store_true", help="Move it back into the data directory")
    mk.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    bots = sub.add_parser("bots", help="List and inspect registered child bots")
    bots_sub = bots.add_subparsers(dest="bots_command")
    bots_sub.add_parser("list", help="List registered child bots (tokens are never shown)")
    bs = bots_sub.add_parser("status", help="Health of every child bot (same view as /platform)")
    bs.add_argument("--attention", action="store_true", help="Only bots that need attention")
    bs.add_argument("--json", action="store_true", help="Machine-readable output")
    bi = bots_sub.add_parser("inspect", help="Health, Business connection, telemetry and errors of one child bot")
    bi.add_argument("bot_id", type=int, metavar="BOT_ID")
    bi.add_argument("--probe", action="store_true",
                    help="Also decrypt the token in memory and call getMe (token is never shown)")
    bi.add_argument("--json", action="store_true", help="Machine-readable output")
    sub.add_parser("backup", help="Back up the database (scripts/backup.sh)")
    res = sub.add_parser("restore", help="Restore from a backup file (scripts/restore.sh)")
    res.add_argument("file", metavar="FILE", help="Path to a .sqlite3 backup or full-platform .tar.gz archive")
    res.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    cfg = sub.add_parser("config", help="Show configuration (the token is never displayed)")
    cfg.add_argument("--edit", action="store_true", help="Edit the .env file")
    doctor = sub.add_parser("doctor", help="Diagnose and check installation health")
    doctor.add_argument("--send-test", action="store_true",
                        help="After confirmation, send a test message to the first configured admin")
    sub.add_parser("version", help="Show version")
    mon = sub.add_parser("monitor", help="Live server/database monitoring (read-only)")
    mon.add_argument("section", nargs="?", default="cpu",
                     choices=["cpu", "memory", "disk", "network", "processes", "database", "telegram"])
    mon.add_argument("--json", action="store_true", help="Machine-readable output")
    mon.add_argument("-w", "--watch", type=float, metavar="SECONDS", help="Refresh continuously")
    dash = sub.add_parser("dashboard", help="Full-screen live health dashboard")
    dash.add_argument("--once", action="store_true", help="Render one frame and exit")
    dash.add_argument("--refresh", type=float, default=2.0, help="Refresh interval (seconds)")
    hc = sub.add_parser("health", help="Health check for cron/monitoring (exit 0 ok, 1 warn, 2 crit)")
    hc.add_argument("--json", action="store_true", help="Machine-readable output")
    un = sub.add_parser("uninstall", help="Uninstall ZIKO (scripts/uninstall.sh)")
    un.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    return p


def _interactive() -> int:
    ui = _ui()

    def restore() -> int:
        file = ui.ask("Backup file path (empty = cancel)")
        return cmd_restore(file) if file else 0

    def config() -> int:
        rc = cmd_config()
        if rc == 0 and ui.confirm("Edit the configuration file?"):
            return cmd_config(edit=True)
        return rc

    def update_center() -> int:
        status = update_status(force=True)
        ui.render_update_center(status)
        choice = ui.ask("Select [0-3]").strip()
        if choice == "0" or not choice:
            return 0
        if choice == "1":
            if not status.get("reachable"):
                ui.error("GitHub could not be reached. No files were changed.")
                return 1
            return cmd_update(yes=True, channel="stable")
        if choice == "2":
            try:
                releases = available_stable_releases()
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, urllib.error.URLError) as exc:
                ui.error(f"Could not retrieve stable releases: {exc}")
                return 1
            if not releases:
                ui.warn("No published stable releases were found.")
                return 1
            ui.render_release_choices(releases)
            selected = ui.ask("Release number (0 = back)").strip()
            if selected == "0" or not selected:
                return 0
            if not selected.isdigit() or not 1 <= int(selected) <= len(releases):
                ui.error("Invalid release selection.")
                return 2
            return cmd_update(yes=True, version=releases[int(selected) - 1][0], channel="stable")
        if choice == "3":
            if not status.get("reachable"):
                ui.error("GitHub could not be reached. No files were changed.")
                return 1
            return cmd_update(yes=True, channel="beta")
        ui.error("Choose 0, 1, 2, or 3.")
        return 2


    def logs_custom() -> int:
        raw = ui.ask("Number of lines (1-5000, empty = cancel)")
        if not raw:
            return 0
        if not (raw.isascii() and raw.isdigit() and 1 <= int(raw) <= 5000):
            ui.warn("Enter a whole number between 1 and 5000.")
            return 1
        return cmd_logs(int(raw))

    from app import cli_home

    env_values, _env_problem = _read_env()
    env_values = env_values or {}
    db = _db_path(env_values)
    token = env_values.get("BOT_TOKEN", "")

    def master_name() -> str | None:
        ok, result = _telegram_api(token, "getMe", timeout=4.0)
        if ok and isinstance(result, dict) and result.get("username"):
            return str(result["username"])
        return None

    collector = cli_home.Collector(data_dir=db.parent, db_path=db,
                                   master_name=master_name if _TOKEN_FORMAT.fullmatch(token) else None)

    def home() -> cli_home.HomeModel:
        import socket
        return collector.collect(service=service_info(), version=_version(), update=update_status(),
                                 host=socket.gethostname(), last_backup_at=_last_backup()[0])

    def bots_status() -> int:
        from app import cli_bots
        return cli_bots.cmd_bots_status(db_path=db)

    def bots_inspect() -> int:
        from app import cli_telegram
        raw = ui.ask("Bot ID (digits, empty = cancel)")
        if not raw:
            return 0
        if not (raw.isascii() and raw.isdigit() and len(raw) <= 15):
            ui.warn("A bot ID is a number, for example 9126874011.")
            return 2
        return cli_telegram.cmd_bot_inspect(int(raw), db_path=db, api=_telegram_api, probe=False,
                                            as_json=False, values=env_values)

    def security_audit() -> int:
        from app import cli_security
        return cli_security.cmd_audit(env_file=_env_path(), db_path=db, values=env_values)

    def security_rotate() -> int:
        from app import cli_security
        return cli_security.cmd_rotate_key(db_path=db, service_state=service_state, yes=False,
                                           values=env_values)

    def security_move() -> int:
        from app import cli_security
        ui.info("1 = move the key OUT of the data directory (recommended)   2 = move it back")
        choice = ui.ask("Select [1-2] (empty = cancel)")
        if choice not in {"1", "2"}:
            return 0
        return cli_security.cmd_move_key(db_path=db, env_file=_env_path(), values=env_values,
                                         service_state=service_state, to_data=choice == "2", yes=False)

    def monitor_live() -> int:
        return cli_home.live(home, console=ui.console)

    actions = {
        "bots_status": bots_status, "bots_inspect": bots_inspect,
        "security_audit": security_audit, "security_rotate": security_rotate,
        "security_move": security_move, "monitor_live": monitor_live,
        "status": cmd_status, "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart,
        "logs_tail": lambda: cmd_logs(50), "logs_follow": lambda: cmd_logs(50, follow=True),
        "logs_custom": logs_custom, "backup": cmd_backup, "restore": restore,
        "update": update_center, "config": config, "doctor": cmd_doctor,
        "help": cmd_help, "bots": cmd_bots_list, "uninstall": cmd_uninstall,
        "tenants": lambda: ui.show_guide("tenants"),
        "autoreply": lambda: ui.show_guide("autoreply"),
        "business": lambda: ui.show_guide("business"),
    }
    return ui.run_menu(actions, service_state, version=_version, version_error_fn=_version_error,
                       update_status_fn=update_status, home_fn=home)


def main(argv: Sequence[str] | None = None) -> int:
    if os.geteuid() != 0:
        print(
            "Access denied: ZIKO management panel is available to root only.\n"
            "Run: sudo ziko",
            file=sys.stderr,
        )
        return 13
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.version or args.command == "version":
            return cmd_version()
        if args.command is None:
            if sys.stdin.isatty() and sys.stdout.isatty():
                return _interactive()
            parser.print_help()
            return 0
        c = args.command
        if c == "logs":
            if args.master or args.bot is not None or args.level:
                return cmd_file_logs(
                    bot_id=args.bot, lines=args.lines, follow=args.follow, level=args.level,
                )
            return cmd_logs(args.lines, args.follow)
        if c == "restore":
            return cmd_restore(args.file, args.yes)
        if c == "config":
            return cmd_config(args.edit)
        if c == "update":
            return _update_dispatch(args)
        if c == "security":
            from app import cli_security
            values, problem = _read_env()
            if problem is not None:
                print("Cannot read configuration; run with sudo.", file=sys.stderr)
                return 1
            if args.security_command == "rotate-key":
                return cli_security.cmd_rotate_key(
                    db_path=_db_path(values), service_state=service_state, yes=args.yes,
                    values=values or {})
            if args.security_command == "move-key":
                return cli_security.cmd_move_key(
                    db_path=_db_path(values), env_file=_env_path(), values=values or {},
                    service_state=service_state, to_data=args.to_data, yes=args.yes)
            return cli_security.cmd_audit(
                env_file=_env_path(), db_path=_db_path(values), values=values or {},
                as_json=getattr(args, "json", False))
        if c == "bots":
            if args.bots_command == "inspect":
                from app import cli_telegram
                values, problem = _read_env()
                if problem is not None:
                    print("Cannot read configuration; run with sudo.", file=sys.stderr)
                    return 1
                return cli_telegram.cmd_bot_inspect(
                    args.bot_id, db_path=_db_path(values), api=_telegram_api,
                    probe=args.probe, as_json=args.json, values=values or {})
            if args.bots_command == "status":
                from app import cli_bots
                values, problem = _read_env()
                if problem is not None:
                    print("Cannot read configuration; run with sudo.", file=sys.stderr)
                    return 1
                return cli_bots.cmd_bots_status(db_path=_db_path(values), as_json=args.json,
                                                attention=args.attention)
            if args.bots_command not in {None, "list"}:
                parser.error("unsupported bots command")
            return cmd_bots_list()
        if c == "uninstall":
            return cmd_uninstall(args.yes)
        if c == "doctor":
            return cmd_doctor(args.send_test)
        if c in {"monitor", "dashboard", "health"}:
            from app import cli_monitor
            values, _ = _read_env()
            db = _db_path(values)
            if c == "monitor" and args.section == "telegram":
                from app import cli_telegram
                return cli_telegram.cmd_monitor_telegram(
                    db_path=db, token=(values or {}).get("BOT_TOKEN", ""), api=_telegram_api,
                    as_json=args.json, watch=args.watch)
            if c == "monitor":
                return cli_monitor.cmd_monitor(args.section, data_dir=db.parent, db_path=db,
                                               as_json=args.json, watch=args.watch)
            if c == "dashboard":
                return cli_monitor.cmd_dashboard(data_dir=db.parent, db_path=db, version=_version(),
                                                 service_state=service_state,
                                                 refresh=args.refresh, once=args.once)
            rc, alerts = cli_monitor.health(db.parent, db)
            if args.json:
                print(json.dumps({"status": rc, "checks": [
                    {"level": a, "metric": m, "detail": d} for a, m, d in alerts]}))
            else:
                for a, m, d in alerts:
                    print(f"{a:<5} {m:<26} {d}")
            return rc
        return {"status": cmd_status, "start": cmd_start, "stop": cmd_stop,
                "restart": cmd_restart, "backup": cmd_backup}[c]()
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
