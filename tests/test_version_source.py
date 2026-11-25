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
