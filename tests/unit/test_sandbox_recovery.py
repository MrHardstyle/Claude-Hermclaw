"""P18 cleanup/recovery helpers without a real engine: time parsing, inspect parsing, a scripted fake engine
CLI (test-only fake) for the list/inspect/remove flow, and stale workspace pruning on the real filesystem."""

from __future__ import annotations

import json
import os
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermclaw.core.errors import ExternalServiceError
from worker.execution.recovery import (
    ContainerRecord,
    RecoveryReport,
    _parse_inspect,
    list_managed_containers,
    parse_engine_time,
    prune_stale_workspaces,
    recover_abandoned,
)

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)


def test_parse_engine_time_formats() -> None:
    assert parse_engine_time("2026-10-09T11:59:00.123456789Z") == datetime(2026, 10, 9, 11, 59, 0, 123456, tzinfo=UTC)
    assert parse_engine_time("2026-10-09T13:59:00+02:00") == datetime(2026, 10, 9, 11, 59, 0, tzinfo=UTC)
    assert parse_engine_time("2026-10-09 11:59:00.5 +0000 UTC") == datetime(2026, 10, 9, 11, 59, 0, 500000, tzinfo=UTC)
    assert parse_engine_time("2026-10-09T11:59:00") == datetime(2026, 10, 9, 11, 59, 0, tzinfo=UTC)
    for bad in (None, "", "yesterday", "2026-13-40T99:99:99Z"):
        assert parse_engine_time(bad) is None


def test_parse_inspect_podman_and_docker_shapes() -> None:
    podman = [
        {
            "Id": "a" * 64,
            "Name": "hermclaw-r1",
            "Created": "2026-10-09T11:00:00.1Z",
            "State": {"Status": "running"},
            "Config": {"Labels": {"hermclaw.managed": "true", "hermclaw.request": "r1"}},
        }
    ]
    docker = {"Id": "b" * 64, "Name": "/hermclaw-r2", "Created": "2026-10-09T11:00:00Z", "State": "exited", "Config": {"Labels": None}}
    recs = _parse_inspect(json.dumps(podman)) + _parse_inspect(json.dumps(docker))
    assert [r.name for r in recs] == ["hermclaw-r1", "hermclaw-r2"]
    assert recs[0].request_id == "r1" and recs[0].state == "running" and recs[1].labels == {} and recs[1].state == "exited"
    assert recs[0].age_seconds(NOW) == pytest.approx(3599.9)
    assert _parse_inspect("not json") == [] and _parse_inspect(json.dumps([{"Name": "no-id"}])) == []
    assert ContainerRecord(id="x", name="x", state="", created_at=None).age_seconds(NOW) is None


def test_report_as_dict() -> None:
    report = RecoveryReport(engine="podman", label="l", scanned=2, removed=["a"], kept={"b": "tracked"})
    assert report.as_dict() == {
        "engine": "podman",
        "label": "l",
        "dry_run": False,
        "scanned": 2,
        "removed": ["a"],
        "kept": {"b": "tracked"},
        "errors": [],
    }


# ---------------------------------------------------------------------------------------------- scripted engine
FAKE_ENGINE = r"""#!/bin/sh
# test-only fake of the podman CLI: state lives in $FAKE_DIR
echo "$*" >> "$FAKE_DIR/calls"
case "$1" in
  ps)
    [ -f "$FAKE_DIR/ps_fail" ] && { echo "Error: cannot connect" >&2; exit 125; }
    cat "$FAKE_DIR/ids" 2>/dev/null; exit 0 ;;
  container)
    shift 1; [ "$1" = inspect ] && shift 1
    [ $# -gt 1 ] && [ -f "$FAKE_DIR/multi_fail" ] && { echo "Error: no such container" >&2; exit 125; }
    printf '['; first=1
    for id in "$@"; do
      [ -f "$FAKE_DIR/c_$id.json" ] || { echo "Error: no such container $id" >&2; exit 125; }
      [ $first = 1 ] || printf ','; first=0; cat "$FAKE_DIR/c_$id.json"
    done
    printf ']'; exit 0 ;;
  kill) exit 0 ;;
  rm)
    name=$(eval echo \${$#})
    [ "$name" = "hermclaw-stuck" ] && { echo "Error: device busy" >&2; exit 2; }
    echo "$name" >> "$FAKE_DIR/removed"; exit 0 ;;
esac
exit 1
"""


@pytest.fixture
def fake_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    state = tmp_path / "state"
    state.mkdir()
    exe = tmp_path / "fake-podman"
    exe.write_text(FAKE_ENGINE)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FAKE_DIR", str(state))
    return str(exe), state


def _container(state: Path, cid: str, name: str, *, created: datetime, status: str = "exited") -> None:
    data = {
        "Id": cid,
        "Name": name,
        "Created": created.isoformat().replace("+00:00", "Z"),
        "State": {"Status": status},
        "Config": {"Labels": {"hermclaw.managed": "true", "hermclaw.request": name.removeprefix("hermclaw-"), "hermclaw.job": "j1"}},
    }
    (state / f"c_{cid}.json").write_text(json.dumps(data))
    with (state / "ids").open("a") as fh:
        fh.write(cid + "\n")


async def test_recover_removes_only_untracked_old_containers(fake_engine: tuple[str, Path]) -> None:
    exe, state = fake_engine
    _container(state, "id1", "hermclaw-old", created=NOW - timedelta(hours=2))
    _container(state, "id2", "hermclaw-young", created=NOW - timedelta(seconds=5), status="running")
    _container(state, "id3", "hermclaw-tracked", created=NOW - timedelta(hours=1), status="running")
    _container(state, "id4", "hermclaw-stuck", created=NOW - timedelta(hours=1))
    report = await recover_abandoned(executable=exe, tracked={"hermclaw-tracked"}, older_than_seconds=60, now=NOW)
    assert report.scanned == 4
    assert report.removed == ["hermclaw-old"]
    assert report.kept == {"hermclaw-young": "younger than 60s", "hermclaw-tracked": "tracked"}
    assert len(report.errors) == 1 and report.errors[0].startswith("hermclaw-stuck: Error: device busy")
    assert (state / "removed").read_text().split() == ["hermclaw-old"]
    calls = (state / "calls").read_text()
    assert "ps -a -q --no-trunc --filter label=hermclaw.managed=true" in calls
    assert "kill -s KILL hermclaw-old" in calls and "rm -f -i -t 0 hermclaw-old" in calls


async def test_dry_run_touches_nothing(fake_engine: tuple[str, Path]) -> None:
    exe, state = fake_engine
    _container(state, "id1", "hermclaw-old", created=NOW - timedelta(hours=2))
    report = await recover_abandoned(executable=exe, dry_run=True, now=NOW)
    assert report.dry_run and report.removed == ["hermclaw-old"]
    assert not (state / "removed").exists() and "kill" not in (state / "calls").read_text()


async def test_vanished_container_between_ps_and_inspect(fake_engine: tuple[str, Path]) -> None:
    exe, state = fake_engine
    _container(state, "id1", "hermclaw-a", created=NOW - timedelta(hours=2))
    with (state / "ids").open("a") as fh:
        fh.write("gone\n")  # listed by ps, removed (--rm) before inspect
    (state / "multi_fail").touch()
    records = await list_managed_containers(exe)
    assert [r.name for r in records] == ["hermclaw-a"]


async def test_list_failure_raises(fake_engine: tuple[str, Path]) -> None:
    exe, state = fake_engine
    (state / "ps_fail").touch()
    with pytest.raises(ExternalServiceError) as exc:
        await recover_abandoned(executable=exe)
    assert exc.value.code == "CONTAINER_LIST_FAILED" and "cannot connect" in exc.value.message


async def test_missing_engine_raises(tmp_path: Path) -> None:
    with pytest.raises(ExternalServiceError):
        await recover_abandoned(executable=str(tmp_path / "no-such-engine"))


async def test_docker_rm_argv(fake_engine: tuple[str, Path]) -> None:
    exe, state = fake_engine
    _container(state, "id1", "hermclaw-old", created=NOW - timedelta(hours=2))
    report = await recover_abandoned(engine="docker", executable=exe, now=NOW)
    assert report.removed == ["hermclaw-old"] and "rm -f hermclaw-old" in (state / "calls").read_text()


# ---------------------------------------------------------------------------------------------- workspaces
def _age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t), follow_symlinks=False)


def test_prune_stale_workspaces(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    old = root / "ws-old"
    (old / "sub").mkdir(parents=True)
    (old / "sub" / "f").write_text("x")
    for p in (old / "sub" / "f", old / "sub", old):
        _age(p, 7200)
    recent = root / "ws-recent-inside"
    (recent / "deep").mkdir(parents=True)
    (recent / "deep" / "new.txt").write_text("fresh")  # recent file deep inside keeps the workspace
    _age(recent / "deep", 7200)
    _age(recent, 7200)
    busy = root / "ws-busy"
    busy.mkdir()
    _age(busy, 7200)
    incoming = root / ".incoming-abc"
    incoming.mkdir()
    _age(incoming, 7200)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("p")
    link = root / "ws-link"
    link.symlink_to(outside)
    _age(link, 7200)
    _age(outside, 7200)

    dry = prune_stale_workspaces(root, older_than_seconds=3600, keep={"ws-busy"}, dry_run=True)
    assert dry == [".incoming-abc", "ws-link", "ws-old"] and old.exists()

    removed = prune_stale_workspaces(root, older_than_seconds=3600, keep={"ws-busy"})
    assert removed == [".incoming-abc", "ws-link", "ws-old"]
    assert not old.exists() and not incoming.exists() and not os.path.lexists(link)
    assert recent.exists() and busy.exists()
    assert (outside / "precious").read_text() == "p"  # the symlink was removed, never followed
    assert prune_stale_workspaces(tmp_path / "missing", older_than_seconds=1) == []
