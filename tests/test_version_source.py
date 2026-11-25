from pathlib import Path
import tomllib

def test_project_version_is_derived_from_canonical_version_file():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert "version" in project["project"]["dynamic"]
    assert project["tool"]["setuptools"]["dynamic"]["version"]["file"] == ["VERSION"]

def test_runtime_version_matches_canonical_version_file():
    import app
    root = Path(__file__).resolve().parents[1]
    assert app.__version__ == (root / "VERSION").read_text(encoding="utf-8").strip().removeprefix("v")


def _run_check(*args: str):
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "check_version.py"), *args],
        capture_output=True, text=True, check=False,
    )


def test_release_tag_must_match_canonical_version():
    root = Path(__file__).resolve().parents[1]
    version = (root / "VERSION").read_text(encoding="utf-8").strip().removeprefix("v")
    assert _run_check("--tag", f"v{version}").returncode == 0
    mismatch = _run_check("--tag", "v9.9.9")
    assert mismatch.returncode != 0
    assert "does not match VERSION" in mismatch.stderr


def test_changelog_has_section_for_canonical_version():
    root = Path(__file__).resolve().parents[1]
    version = (root / "VERSION").read_text(encoding="utf-8").strip().removeprefix("v")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{version}]" in changelog


def test_print_returns_canonical_version_only():
    root = Path(__file__).resolve().parents[1]
    version = (root / "VERSION").read_text(encoding="utf-8").strip().removeprefix("v")
    result = _run_check("--print")
    assert result.returncode == 0
    assert result.stdout.strip() == version
