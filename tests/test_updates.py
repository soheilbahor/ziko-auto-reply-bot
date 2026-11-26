"""`ziko update` check / history / verify / rollback (app/updates.py, app/cli_updates.py)."""
from __future__ import annotations

import contextlib
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import cli_updates, updates

ROOT_FILES = {"VERSION": "1.5.0\n", "app/main.py": "x = 1\n", "app/storage.py": "SCHEMA_VERSION = 4\n",
              "requirements.txt": "aiogram==3.34.0\n", "pyproject.toml": "[project]\n",
              "scripts/update.sh": "#!/bin/bash\n", "scripts/backup.sh": "#!/bin/bash\n",
              "scripts/restore-platform.sh": "#!/bin/bash\n", "scripts/verify_platform.py": "y = 2\n",
              "deploy/ziko.service": "[Unit]\n"}


def make_tree(root: Path, version: str = "1.5.0") -> Path:
    for rel, content in ROOT_FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(version + "\n" if rel == "VERSION" else content)
    (root / ".venv" / "bin").mkdir(parents=True)          # never part of the manifest
    (root / ".venv" / "bin" / "python").write_text("")
    return root


def release_json(tag="v1.6.0", assets=("SHA256SUMS", "ziko-assistant-v1.6.0.tar.gz"), body="Fixes.", pre=False):
    return {"tag_name": tag, "name": f"ZIKO {tag}", "published_at": "2026-11-20T10:00:00Z", "body": body,
            "html_url": f"https://github.com/o/r/releases/tag/{tag}", "prerelease": pre,
            "assets": [{"name": a} for a in assets]}


ERR_AWARE = (cli_updates.cmd_check, cli_updates.cmd_verify, cli_updates.cmd_rollback)


def run(fn, **kw):
    out, err = io.StringIO(), io.StringIO()
    if fn in ERR_AWARE:
        kw.setdefault("err", err)
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(**kw)
    return rc, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("a,b,newer", [
    ("1.6.0", "1.5.0", True), ("v1.5.1", "1.5.0", True), ("1.5.0", "1.5.0", False),
    ("1.5.0", "1.5.0-rc1", True), ("1.5.0-rc2", "1.5.0-rc1", True), ("1.10.0", "1.9.9", True),
    ("1.4.9", "1.5.0", False), ("garbage", "1.5.0", False),
])
def test_version_ordering(a, b, newer):
    assert updates.is_newer(a, b) is newer


def test_release_notes_are_sanitised_for_the_terminal():
    nasty = "Fix\x1b[2J\x1b]0;pwned\x07 bug\x07\x00 ok \x9b31m"
    cleaned = updates.clean_text(nasty)
    assert "\x1b" not in cleaned and "\x07" not in cleaned and "\x00" not in cleaned and "\x9b" not in cleaned
    assert "Fix" in cleaned and "bug" in cleaned and "ok" in cleaned
    assert updates.clean_text("x" * 5000, limit=100).endswith("(truncated)")


def test_latest_release_parsing_and_validation():
    rel = updates.latest_release("o/r", fetch=lambda url, timeout: release_json())
    assert rel.tag == "v1.6.0" and "SHA256SUMS" in rel.assets
    with pytest.raises(updates.UpdateError, match="invalid repository"):
        updates.latest_release("o/r; rm -rf /", fetch=lambda *a: release_json())
    with pytest.raises(updates.UpdateError, match="invalid tag"):
        updates.latest_release("o/r", fetch=lambda *a: release_json(tag="v1.6.0;evil"))
    with pytest.raises(updates.UpdateError, match="unexpected"):
        updates.latest_release("o/r", fetch=lambda *a: ["not", "a", "dict"])


def test_check_flags_unverifiable_releases():
    ok = updates.check("1.5.0", updates.latest_release("o/r", fetch=lambda *a: release_json()))
    assert ok.update_available and ok.verifiable and not ok.problems
    bad = updates.check("1.5.0", updates.latest_release("o/r", fetch=lambda *a: release_json(assets=("other.bin",))))
    assert not bad.verifiable and any("SHA256SUMS" in p for p in bad.problems)


def test_cli_check_exit_codes_and_json():
    fetch = lambda *a: release_json()  # noqa: E731
    rc, out, _ = run(cli_updates.cmd_check, installed="1.5.0", repo="o/r", fetch=fetch)
    assert rc == cli_updates.EXIT_UPDATE_AVAILABLE and "An update is available" in out and "Fixes." in out
    rc, out, _ = run(cli_updates.cmd_check, installed="1.6.0", repo="o/r", fetch=fetch)
    assert rc == 0 and "up to date" in out
    rc, out, _ = run(cli_updates.cmd_check, installed="1.5.0", repo="o/r", fetch=fetch, as_json=True)
    data = json.loads(out)
    assert data["update_available"] and data["latest"] == "v1.6.0" and rc == cli_updates.EXIT_UPDATE_AVAILABLE

    def offline(*_a):
        raise updates.UpdateError("cannot reach api.github.com (network, DNS or TLS problem)")
    rc, _out, err = run(cli_updates.cmd_check, installed="1.5.0", repo="o/r", fetch=offline)
    assert rc == 1 and "cannot reach api.github.com" in err


def test_interactive_update_never_installs_without_consent(monkeypatch):
    calls = []
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # not a TTY
    rc, out, _ = run(cli_updates.cmd_interactive, installed="1.5.0", repo="o/r",
                     fetch=lambda *a: release_json(), run_update=lambda channel: calls.append(channel) or 0)
    assert rc == 1 and calls == [] and "nothing installed" in out
    rc, _o, _e = run(cli_updates.cmd_interactive, installed="1.5.0", repo="o/r", yes=True,
                     fetch=lambda *a: release_json(), run_update=lambda channel: calls.append(channel) or 0)
    assert rc == 0 and calls == ["stable"]
    calls.clear()
    rc, _o, _e = run(cli_updates.cmd_interactive, installed="1.5.0", repo="o/r", yes=True,
                     fetch=lambda *a: release_json(assets=()), run_update=lambda channel: calls.append(channel) or 0)
    assert calls == []  # an unverifiable release is never installed, even with --yes


def journal(tmp_path, *events):
    path = updates.journal_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events) + "{broken line\n")
    return path


def test_history_groups_runs_and_outcomes(tmp_path):
    journal(tmp_path,
            {"ts": "2026-11-01T10:00:00Z", "run": "A", "stage": "UPDATE_START"},
            {"ts": "2026-11-01T10:00:01Z", "run": "A", "stage": "PLAN", "from": "1.4.3", "to": "1.5.0", "channel": "stable"},
            {"ts": "2026-11-01T10:02:00Z", "run": "A", "stage": "UPDATE_COMPLETE"},
            {"ts": "2026-11-02T10:00:00Z", "run": "B", "stage": "PLAN", "from": "1.5.0", "to": "1.5.1", "channel": "stable"},
            {"ts": "2026-11-02T10:01:00Z", "run": "B", "stage": "FAILED", "failed_stage": "HEALTH_CHECK", "message": "post-update health check failed"},
            {"ts": "2026-11-02T10:01:01Z", "run": "B", "stage": "ROLLBACK", "failed_stage": "HEALTH_CHECK"},
            {"ts": "2026-11-02T10:01:30Z", "run": "B", "stage": "ROLLBACK_DONE"},
            {"ts": "2026-11-03T10:00:00Z", "run": "C", "stage": "PLAN", "kind": "rollback", "from": "1.5.0", "to": "1.4.3"},
            {"ts": "2026-11-03T10:01:00Z", "run": "C", "stage": "ROLLBACK_COMPLETE"},
            {"ts": "2026-11-04T10:00:00Z", "run": "D", "stage": "PLAN", "from": "1.4.3", "to": "1.5.0"})
    history = updates.runs(updates.read_journal(updates.journal_path(tmp_path)), now=1e12)
    by_id = {r.run: r for r in history}
    assert [r.run for r in history] == ["D", "C", "B", "A"]
    assert by_id["A"].outcome == "success" and by_id["A"].to_version == "1.5.0"
    assert by_id["B"].outcome == "rolled_back" and by_id["B"].failed_stage == "HEALTH_CHECK"
    assert by_id["C"].outcome == "rollback"
    assert by_id["D"].outcome == "interrupted"
    rc, out, _ = run(cli_updates.cmd_history, data_dir=tmp_path, as_json=True)
    rows = {r["run"]: r for r in json.loads(out)}
    assert rc == 0 and rows["B"]["outcome"] == "rolled_back" and rows["B"]["failed_stage"] == "HEALTH_CHECK"
    rc, out, _ = run(cli_updates.cmd_history, data_dir=tmp_path)
    assert rc == 0 and "rolled" in out and "HEALTH_CHECK" in out   # table may wrap words
    rc, out, _ = run(cli_updates.cmd_logs, data_dir=tmp_path, run_id="B")
    assert "post-update health check failed" in out
    assert run(cli_updates.cmd_logs, data_dir=tmp_path, run_id="nope")[0] == 1


def test_history_without_journal(tmp_path):
    rc, out, _ = run(cli_updates.cmd_history, data_dir=tmp_path)
    assert rc == 0 and "No update history" in out


def test_verify_detects_tampering(tmp_path):
    root = make_tree(tmp_path / "opt")
    updates.write_manifest(root, "1.5.0")
    report = updates.verify(root)
    assert report.ok and report.checked == len(ROOT_FILES)
    (root / "app" / "main.py").write_text("import os; os.system('evil')\n")
    (root / "app" / "backdoor.py").write_text("x = 1\n")
    (root / "scripts" / "backup.sh").unlink()
    report = updates.verify(root)
    text = "\n".join(report.problems)
    assert not report.ok
    assert "modified: app/main.py" in text and "unexpected file: app/backdoor.py" in text
    assert "missing: scripts/backup.sh" in text
    rc, out, _ = run(cli_updates.cmd_verify, root=root)
    assert rc == 2 and "modified: app/main.py" in out


def test_verify_detects_syntax_errors_and_version_drift(tmp_path):
    root = make_tree(tmp_path / "opt")
    updates.write_manifest(root, "1.5.0")
    (root / "VERSION").write_text("1.6.0\n")
    (root / "scripts" / "verify_platform.py").write_text("def broken(:\n")
    text = "\n".join(updates.verify(root).problems)
    assert "manifest is for '1.5.0'" in text and "does not compile: scripts/verify_platform.py" in text


def test_verify_without_manifest_warns_and_record_creates_it(tmp_path):
    root = make_tree(tmp_path / "opt")
    report = updates.verify(root)
    assert report.ok and report.warnings and report.checked == 0
    rc, out, _ = run(cli_updates.cmd_verify, root=root, record=True)
    assert rc == 0 and (root / updates.MANIFEST_NAME).is_file()
    assert updates.verify(root).checked == len(ROOT_FILES)
    (root / "app" / "main.py").unlink()
    rc, _o, err = run(cli_updates.cmd_verify, root=root, record=True)
    assert rc == 2 and "required file missing" in err  # never record a broken install


def make_snapshot(backup_dir: Path, version: str, stamp: str, *, extra=None, drop=None) -> Path:
    src = make_tree(backup_dir.parent / f"src-{version}-{stamp}", version)
    if drop:
        (src / drop).unlink()
    path = backup_dir / f"code-{version}-{stamp}-123.tar.gz"
    backup_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        for p in sorted(src.rglob("*")):
            rel = p.relative_to(src)
            if rel.parts[0] == ".venv":
                continue
            tar.add(p, arcname="./" + rel.as_posix(), recursive=False)
        if extra:
            for name, kind in extra:
                info = tarfile.TarInfo(name)
                if kind == "symlink":
                    info.type, info.linkname = tarfile.SYMTYPE, "/etc/shadow"
                    tar.addfile(info)
                else:
                    data = b"x"
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
    return path


def test_snapshots_listing_and_plan(tmp_path):
    bk = tmp_path / "bk"
    make_snapshot(bk, "1.4.3", "20261101T100000")
    make_snapshot(bk, "1.4.2", "20261001T100000")
    make_snapshot(bk, "1.5.0", "20261120T100000")
    (bk / "code-not-a-snapshot.tar.gz").write_bytes(b"x")
    snaps = updates.snapshots(bk)
    assert [s.version for s in snaps] == ["1.5.0", "1.4.3", "1.4.2"]
    assert updates.rollback_plan(bk, "1.5.0").version == "1.4.3"      # newest OTHER version
    assert updates.rollback_plan(bk, "1.5.0", "1.4.2").version == "1.4.2"
    with pytest.raises(updates.UpdateError, match="no code snapshot of version 9.9.9"):
        updates.rollback_plan(bk, "1.5.0", "9.9.9")
    with pytest.raises(updates.UpdateError, match="no earlier version"):
        updates.rollback_plan(tmp_path / "empty", "1.5.0")


@pytest.mark.parametrize("extra,drop,needle", [
    ([("../../etc/cron.d/x", "file")], None, "unsafe path"),
    ([("/etc/passwd", "file")], None, "unsafe path"),
    ([("./app/link", "symlink")], None, "link or device"),
    ([("./.env", "file")], None, "excluded entry"),
    (None, "app/storage.py", "missing app/storage.py"),
])
def test_unsafe_or_incomplete_snapshots_are_refused(tmp_path, extra, drop, needle):
    snap = make_snapshot(tmp_path / "bk", "1.4.3", "20261101T100000", extra=extra, drop=drop)
    assert any(needle in p for p in updates.check_snapshot(snap))


def test_corrupt_snapshot_is_refused(tmp_path):
    bk = tmp_path / "bk"
    bk.mkdir()
    bad = bk / "code-1.4.3-20261101T100000-1.tar.gz"
    bad.write_bytes(b"not gzip")
    assert updates.check_snapshot(bad) == ["snapshot cannot be read (corrupt or not a .tar.gz)"]


def test_cli_rollback_delegates_to_update_sh_only_after_validation(tmp_path, monkeypatch):
    root = make_tree(tmp_path / "opt")
    bk = tmp_path / "bk"
    good = make_snapshot(bk, "1.4.3", "20261101T100000")
    calls = []
    runner = lambda cmd, check: calls.append(cmd) or SimpleNamespace(returncode=0)  # noqa: E731
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc, out, _ = run(cli_updates.cmd_rollback, root=root, backup_dir=bk, installed="1.5.0", runner=runner)
    assert rc == 1 and calls == [] and "Cancelled" in out                    # no TTY, no --yes
    rc, out, _ = run(cli_updates.cmd_rollback, root=root, backup_dir=bk, installed="1.5.0", runner=runner, yes=True)
    assert rc == 0 and calls == [["bash", str(root / "scripts" / "update.sh"), "--rollback-to", str(good)]]
    assert "databases     : kept" in out
    calls.clear()
    run(cli_updates.cmd_rollback, root=root, backup_dir=bk, installed="1.5.0", runner=runner, yes=True,
        with_data=True)
    assert calls[0][-1] == "--with-data"
    calls.clear()
    make_snapshot(bk, "1.4.9", "20261110T100000", extra=[("../evil", "file")])
    rc, _o, err = run(cli_updates.cmd_rollback, root=root, backup_dir=bk, installed="1.5.0", runner=runner, yes=True)
    assert rc == 2 and calls == [] and "failed validation" in err           # newest is unsafe: refused
    failing = lambda cmd, check: SimpleNamespace(returncode=1)  # noqa: E731
    rc, out, _ = run(cli_updates.cmd_rollback, root=root, backup_dir=bk, installed="1.5.0", runner=failing,
                     yes=True, target="1.4.3")
    assert rc == 1 and "Rollback failed" in out


def test_update_sh_rollback_mode_has_the_safeguards():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "update.sh").read_text()
    body = script[script.index("manual_rollback() {"):script.index("cleanup() {")]
    order = [body.index(s) for s in ("stage ROLLBACK_VALIDATE", "stage BACKUP_START", "stage CODE_SNAPSHOT",
                                     "stage SERVICE_STOP", "stage FILES_DEPLOY", "stage HEALTH_CHECK",
                                     "stage ROLLBACK_COMPLETE")]
    assert order == sorted(order)                                            # validate and back up first
    assert 'must be in $BACKUP_DIR' in body and "symlink" in body
    assert "is newer than" in body and body.index("is newer than") < body.index("stage SERVICE_STOP")
    assert "trap 'on_error $LINENO' ERR" in body                             # failures roll back
    assert "--rollback-to) [[ $# -ge 2 ]]" in script and 'flock -n 9' in script
    assert script.index('flock -n 9') < script.index('manual_rollback "$ROLLBACK_TO"')  # same lock as updates
    assert 'journal FAILED "failed_stage=$STAGE_NAME"' in script
    # rolling back to a release without key_location.py while the key is external is refused early
    assert '[[ ! -f "$WORK/rb/app/key_location.py" ]]' in body
    assert body.index("move-key --to-data") < body.index("stage SERVICE_STOP")


# --------------------------------------------------------------------------- 1.5.1 permissions
# Found on a real 1.4.2 -> 1.5.0 update: under `umask 077` the staged release was 0600 root,
# so (a) the staged backup.sh, run as the service user, failed with "Permission denied" and
# (b) after deploy the service (User=ziko) could not read /opt/ziko-assistant/VERSION and
# the update rolled back. These checks pin the fix.
def _update_sh() -> str:
    return (Path(__file__).resolve().parents[1] / "scripts" / "update.sh").read_text()


def _main_body(script: str) -> str:
    return script[script.index("main() {"):]


def test_update_works_from_a_directory_the_service_user_cannot_read():
    body = _main_body(_update_sh())
    assert 'cd / || die "cannot change to /"' in body
    assert body.index('cd / ||') < body.index("WORK=\"$(mktemp -d)\"")


def test_work_dir_is_traverse_only_not_listable():
    body = _main_body(_update_sh())
    mk = body.index("WORK=\"$(mktemp -d)\"")
    assert body.index('chmod 711 "$WORK"') > mk
    assert 'chmod 755 "$WORK"' not in body and 'chmod -R u=rwX,go=rX "$WORK"\n' not in body


def test_staged_release_is_made_readable_before_the_service_user_runs_it():
    body = _main_body(_update_sh())
    fix = body.index('chmod -R u=rwX,go=rX "$WORK/stage"')
    assert fix > body.index("stage STAGE_VALIDATE")
    assert fix < body.index("stage BACKUP_START")                 # before backup.sh runs as ziko
    assert fix < body.index('bash "$WORK/stage/scripts/backup.sh"')


def test_deployed_code_is_root_owned_and_world_readable():
    body = _main_body(_update_sh())
    deploy = body[body.index("stage FILES_DEPLOY"):body.index("stage UNIT_INSTALL")]
    assert deploy.index('chown -R root:root "$ROOT/$name"') < deploy.index('chmod -R u=rwX,go=rX "$ROOT/$name"')
    assert deploy.index('chmod -R u=rwX,go=rX "$ROOT/$name"') < deploy.index("diff -rq")
    assert "go+w" not in deploy and "a+w" not in deploy and "777" not in deploy


def test_manual_rollback_also_restores_readable_permissions():
    script = _update_sh()
    body = script[script.index("manual_rollback() {"):script.index("cleanup() {")]
    deploy = body[body.index("stage FILES_DEPLOY"):body.index("stage DEPENDENCIES_INSTALL")]
    assert 'chmod -R u=rwX,go=rX "$ROOT/$(basename "$rb_entry")"' in deploy


def test_permission_rule_gives_the_service_user_read_but_never_write(tmp_path):
    """The exact chmod rule, applied to files created under umask 077 like the stage."""
    import os
    import stat
    import subprocess
    tree = tmp_path / "stage"
    (tree / "app").mkdir(parents=True)
    old = os.umask(0o077)
    try:
        (tree / "VERSION").write_text("1.5.1\n")
        (tree / "app" / "main.py").write_text("x = 1\n")
        (tree / "install.sh").write_text("#!/bin/bash\n")
        os.chmod(tree / "install.sh", 0o700)
    finally:
        os.umask(old)
    assert stat.S_IMODE((tree / "VERSION").stat().st_mode) == 0o600          # the bug
    subprocess.run(["chmod", "-R", "u=rwX,go=rX", str(tree)], check=True)
    mode = lambda p: stat.S_IMODE(p.stat().st_mode)  # noqa: E731
    assert mode(tree / "VERSION") == 0o644 and mode(tree / "app" / "main.py") == 0o644
    assert mode(tree / "app") == 0o755 and mode(tree) == 0o755
    assert mode(tree / "install.sh") == 0o755                                # executables keep x
