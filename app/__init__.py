"""ZIKO Personal Assistant: Telegram Business auto-reply assistant."""

from importlib.metadata import PackageNotFoundError, version as _distribution_version
from pathlib import Path


def _load_version() -> str:
    marker = Path(__file__).resolve().parent.parent / "VERSION"
    if marker.is_file():
        value = marker.read_text(encoding="utf-8").strip().removeprefix("v")
        if value:
            return value
        raise RuntimeError(f"VERSION file is empty: {marker}")
    try:
        return _distribution_version("ziko-assistant")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _load_version()
