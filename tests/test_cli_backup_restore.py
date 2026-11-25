from pathlib import Path


def test_cli_recognizes_encrypted_platform_archive():
    source = (Path(__file__).resolve().parents[1] / "app/cli.py").read_text()
    assert 'path.name.endswith(".tar.gz.age")' in source
    assert 'path.name.endswith(".sqlite3.age")' in source
