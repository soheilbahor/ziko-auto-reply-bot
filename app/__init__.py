"""ZIKO Personal Assistant: Telegram Business auto-reply assistant."""

import os
from importlib.metadata import PackageNotFoundError, version as _distribution_version
from pathlib import Path


def _read_marker(marker: Path) -> str | None:
    if not marker.is_file():
        return None
    value = marker.read_text(encoding="utf-8").strip().removeprefix("v")
    if not value:
        raise RuntimeError(f"VERSION file is empty: {marker}")
    return value


def _load_version() -> str:
    """Single source of truth: the VERSION file.

    Order: the checkout that contains this package, then the installed tree
    (ZIKO_HOME, the same marker the ``ziko`` CLI reads), then package metadata.
    Metadata is only a last resort, because a stale installed dist-info once
    made the running service report the previous release.
    """
    value = _read_marker(Path(__file__).resolve().parent.parent / "VERSION")
    if value:
        return value
    home = Path(os.environ.get("ZIKO_HOME", "/opt/ziko-assistant"))
    value = _read_marker(home / "VERSION")
    if value:
        return value
    try:
        return _distribution_version("ziko-assistant")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _load_version()
