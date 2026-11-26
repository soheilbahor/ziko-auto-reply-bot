"""Where the child-bot token key lives, and moving it safely.

Two supported layouts:

* legacy (default): ``<data dir>/tenant-token.key``, owned by the service user, 0600.
  The service creates it on first use. It sits next to the databases and is copied
  into every platform backup.
* external: the path in ``ZIKO_TENANT_KEY_FILE`` -- normally
  ``/etc/ziko-assistant/tenant-token.key``, owned ``root:ziko``, mode 0640.
  The service can read it but not modify or delete it (``ProtectSystem=strict``
  keeps /etc read-only for the unit), and platform backups exclude it.

``move_to_config`` / ``move_to_data`` switch between the layouts. Each step is
idempotent, so an interrupted move is completed by running the command again:

  1 verify every stored token decrypts with the current key
  2 publish the key at the new path (hard link: never overwrites an existing key)
  3 verify every token decrypts with the new copy
  4 point the configuration at the new path (atomic rewrite of ziko.env)
  5 remove the old copy
"""
from __future__ import annotations

import os
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

ENV_KEY = "ZIKO_TENANT_KEY_FILE"
KEY_NAME = "tenant-token.key"


class KeyLocationError(RuntimeError):
    """The message never contains key material."""


def legacy_path(data_dir: Path) -> Path:
    return data_dir / KEY_NAME


def resolve(data_dir: Path, values: Mapping[str, str] | None = None) -> tuple[Path, bool]:
    """Return (key path, is_external). Process environment wins over ``values``."""
    raw = os.environ.get(ENV_KEY, "").strip() or (values or {}).get(ENV_KEY, "").strip()
    if not raw:
        return legacy_path(data_dir), False
    path = Path(raw)
    if not path.is_absolute():
        raise KeyLocationError(f"{ENV_KEY} must be an absolute path")
    return path, path != legacy_path(data_dir)


def check_external_file(path: Path) -> None:
    """Fail closed on anything but a regular file that others cannot read."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        raise KeyLocationError(
            f"tenant key not found at {path}; restore it or run: sudo ziko security move-key"
        ) from None
    import stat as _stat
    if not _stat.S_ISREG(st.st_mode):
        raise KeyLocationError(f"tenant key at {path} is not a regular file (symlinks are refused)")
    if st.st_mode & 0o007:
        raise KeyLocationError(f"tenant key at {path} is accessible to other users; chmod 640 it")


# --------------------------------------------------------------------------- #
# ziko.env editing
# --------------------------------------------------------------------------- #
def set_env_value(env_file: Path, key: str, value: str | None, *,
                  chown: Callable[[str, int, int], None] = os.chown) -> None:
    """Set (or with ``None`` remove) KEY in a KEY=VALUE file, atomically.

    Comments, order and every other line are kept. Owner and mode are preserved.
    """
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
        raise ValueError("invalid key name")
    if value is not None and ("\n" in value or "\r" in value):
        raise ValueError("value must be a single line")
    st = env_file.stat()
    pattern = re.compile(rf"^\s*(export\s+)?{re.escape(key)}\s*=")
    lines = [line for line in env_file.read_text(encoding="utf-8").splitlines()
             if not pattern.match(line)]
    if value is not None:
        lines.append(f"{key}={value}")
    tmp = env_file.with_name(f".{env_file.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        try:
            chown(str(tmp), st.st_uid, st.st_gid)
        except PermissionError:
            pass
        os.chmod(tmp, st.st_mode & 0o777)
        os.replace(tmp, env_file)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- #
# moving
# --------------------------------------------------------------------------- #
def _publish(src_bytes: bytes, dest: Path, *, uid: int, gid: int, mode: int,
             chown: Callable[[str, int, int], None],
             link: Callable[[str, str], None]) -> None:
    """Create ``dest`` with exact content/owner/mode, or fail if it already exists."""
    tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(src_bytes)
    try:
        os.chmod(tmp, 0o600)
        chown(str(tmp), uid, gid)
        os.chmod(tmp, mode)
        link(str(tmp), str(dest))  # atomic and never replaces an existing file
    finally:
        tmp.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class MoveResult:
    tokens: int
    key_path: Path
    already_done: bool = False


def _count(verify: Callable[[Path, Path], int], registry: Path, key: Path) -> int:
    return verify(registry, key) if registry.is_file() else 0


def move_to_config(*, registry: Path, data_dir: Path, dest: Path, env_file: Path, gid: int,
                   verify: Callable[[Path, Path], int],
                   chown: Callable[[str, int, int], None] = os.chown,
                   link: Callable[[str, str], None] = os.link) -> MoveResult:
    legacy = legacy_path(data_dir)
    if not dest.is_absolute() or dest == legacy:
        raise KeyLocationError("destination must be an absolute path outside the data directory")
    if not legacy.is_file():
        if dest.is_file():  # earlier run finished steps 1-3 and maybe 4-5
            n = _count(verify, registry, dest)
            set_env_value(env_file, ENV_KEY, str(dest), chown=chown)
            return MoveResult(n, dest, already_done=True)
        raise KeyLocationError(f"no tenant key at {legacy} or {dest}")
    data = legacy.read_bytes()
    n = _count(verify, registry, legacy)                                    # 1
    if dest.exists() or dest.is_symlink():
        if dest.is_symlink() or not dest.is_file() or dest.read_bytes() != data:
            raise KeyLocationError(
                f"a DIFFERENT key already exists at {dest}; refusing to overwrite it")
    else:
        _publish(data, dest, uid=0, gid=gid, mode=0o640, chown=chown, link=link)  # 2
    if _count(verify, registry, dest) != n:                                 # 3
        raise KeyLocationError("token count changed while moving the key")
    set_env_value(env_file, ENV_KEY, str(dest), chown=chown)                # 4
    legacy.unlink()                                                         # 5
    return MoveResult(n, dest)


def move_to_data(*, registry: Path, data_dir: Path, current: Path, env_file: Path,
                 uid: int, gid: int, verify: Callable[[Path, Path], int],
                 chown: Callable[[str, int, int], None] = os.chown,
                 link: Callable[[str, str], None] = os.link) -> MoveResult:
    legacy = legacy_path(data_dir)
    if current == legacy:
        raise KeyLocationError("the key is already in the data directory")
    if not current.is_file():
        if legacy.is_file():
            n = _count(verify, registry, legacy)
            set_env_value(env_file, ENV_KEY, None, chown=chown)
            return MoveResult(n, legacy, already_done=True)
        raise KeyLocationError(f"no tenant key at {current} or {legacy}")
    data = current.read_bytes()
    n = _count(verify, registry, current)
    if legacy.exists() or legacy.is_symlink():
        if legacy.is_symlink() or legacy.read_bytes() != data:
            raise KeyLocationError(f"a DIFFERENT key already exists at {legacy}; refusing to overwrite it")
    else:
        _publish(data, legacy, uid=uid, gid=gid, mode=0o600, chown=chown, link=link)
    if _count(verify, registry, legacy) != n:
        raise KeyLocationError("token count changed while moving the key")
    set_env_value(env_file, ENV_KEY, None, chown=chown)
    current.unlink()
    return MoveResult(n, legacy)
