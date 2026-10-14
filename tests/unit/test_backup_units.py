"""P33 unit tests: configuration, rsync/ssh command lines, listing/itemize parsing, retention planning, archive
safety and secret detection, manifest signing, libpq credential handling, TOC filtering, locks."""

from __future__ import annotations

import io
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from hermclaw.backup.archive import (
    ArchiveSource,
    build_tarball,
    extract_tarball,
    inspect_tarball,
    is_excluded,
    member_name_problem,
    plaintext_secret_reason,
)
from hermclaw.backup.backup import referenced_secret_files
from hermclaw.backup.cli import _set_arg
from hermclaw.backup.config import (
    ALWAYS_EXCLUDE,
    BackupConfig,
    LocalTarget,
    RetentionPolicy,
    RsyncTarget,
    find_backup_config,
    parse_backup_config,
    resolve_rsync_endpoint,
)
from hermclaw.backup.errors import BackupError
from hermclaw.backup.events import _flat
from hermclaw.backup.lock import backup_lock
from hermclaw.backup.manifest import check_set_id, is_set_id, new_set_id, set_time, sign
from hermclaw.backup.pg import PgConn, filter_toc, find_pg_tool, pg_tool_env
from hermclaw.backup.retention import plan_retention
from hermclaw.backup.transfer import RsyncStore, SetListing, itemized_differences, parse_listing, ssh_command, ssh_key_file
from hermclaw.core.config import HostsConfig

ROOT = Path(__file__).resolve().parents[2]
SET = "hermclaw-20261014T023000Z-a1b2c3"


# ================================================================================================ config
def test_parse_local_and_rsync_targets() -> None:
    cfg = parse_backup_config({"backup": {"target": {"kind": "local", "path": "/mnt/terra/hermclaw"}}})
    assert isinstance(cfg.target, LocalTarget) and cfg.target.create_missing is False
    cfg = parse_backup_config(
        {"target": {"kind": "rsync", "host_id": "terra-60", "path": "/mnt/user/backups/hermclaw"}, "retention": {"keep_daily": 14}}
    )
    assert isinstance(cfg.target, RsyncTarget) and cfg.retention.keep_daily == 14 and cfg.retention.keep_weekly == 4


@pytest.mark.parametrize(
    "target",
    [
        {"kind": "local", "path": "relative/dir"},
        {"kind": "rsync", "path": "/a b"},
        {"kind": "rsync", "path": "/x/../etc"},
        {"kind": "rsync", "path": "-e touch"},
        {"kind": "rsync", "path": "/ok", "host": "-oProxyCommand=x"},
        {"kind": "rsync", "path": "/ok", "host": "a;b"},
        {"kind": "rsync", "path": "/ok", "host": "h", "user": "root;id"},
        {"kind": "rsync", "path": "/ok", "known_hosts": "/a b"},
        {"kind": "rsync", "path": "/ok", "ssh_binary": "ssh -oProxyCommand=x"},
        {"kind": "rsync", "path": "host:/x"},
        {"kind": "ftp", "path": "/x"},
    ],
)
def test_invalid_targets_are_rejected(target: dict[str, object]) -> None:
    with pytest.raises(BackupError) as err:
        parse_backup_config({"target": target})
    assert err.value.code == "BACKUP_CONFIG_INVALID"


def test_config_rejects_unknown_keys_plaintext_credstores_and_bad_identifiers() -> None:
    base = {"target": {"kind": "local", "path": "/mnt/x"}}
    for extra in (
        {"unknown": 1},
        {"credstore_dirs": ["/etc/credstore"]},
        {"credstore_dirs": ["/etc/hermclaw/secrets"]},
        {"credstore_dirs": ["relative"]},
        {"record_exports": ["research_runs; DROP TABLE x"]},
        {"restore_test": {"db_prefix": "Bad-Prefix"}},
        {"restore_test": {"template": 'x"; DROP'}},
        {"staging_dir": "relative"},
    ):
        with pytest.raises(BackupError):
            parse_backup_config({**base, **extra})
    ok = parse_backup_config({**base, "credstore_dirs": ["/etc/credstore.encrypted"]})
    assert ok.credstore_dirs == [Path("/etc/credstore.encrypted")]


def test_example_config_is_valid_and_never_used_implicitly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    example = yaml.safe_load((ROOT / "infra" / "backup" / "backup.example.yaml").read_text())
    cfg = parse_backup_config(example)
    assert isinstance(cfg.target, RsyncTarget) and cfg.target.host_id == "terra-60"
    from hermclaw.core.settings import Settings

    monkeypatch.delenv("HERMCLAW_BACKUP_CONFIG", raising=False)
    (tmp_path / "backup.example.yaml").write_text("target: {kind: local, path: /x}\n")
    with pytest.raises(BackupError, match="no backup configuration"):
        find_backup_config(None, Settings(config_dir=tmp_path))
    (tmp_path / "backup.yaml").write_text("target: {kind: local, path: /x}\n")
    assert find_backup_config(None, Settings(config_dir=tmp_path)) == tmp_path / "backup.yaml"


def _hosts() -> HostsConfig:
    return HostsConfig.model_validate(yaml.safe_load((ROOT / "config" / "hosts.example.yaml").read_text()))


def test_rsync_endpoint_from_hosts_yaml_terra_60() -> None:
    target = RsyncTarget(host_id="terra-60", path="/mnt/user/backups/hermclaw")
    endpoint = resolve_rsync_endpoint(target, _hosts())
    assert endpoint.host == "192.168.178.60" and endpoint.remote and endpoint.port == 22
    assert endpoint.key_ref == "file:/etc/hermclaw/ssh/backup_ed25519" and endpoint.user
    assert endpoint.location(SET, slash=True) == f"{endpoint.user}@192.168.178.60:/mnt/user/backups/hermclaw/{SET}/"
    override = resolve_rsync_endpoint(RsyncTarget(host_id="terra-60", path="/b", user="hermclaw-backup", port=2222), _hosts())
    assert (override.user, override.port) == ("hermclaw-backup", 2222)
    with pytest.raises(BackupError, match=r"not in hosts\.yaml"):
        resolve_rsync_endpoint(RsyncTarget(host_id="nope", path="/b"), _hosts())
    with pytest.raises(BackupError, match="dedicated key"):
        resolve_rsync_endpoint(RsyncTarget(host="192.168.178.60", user="root", path="/b"), None)
    with pytest.raises(BackupError, match="absolute path"):
        resolve_rsync_endpoint(RsyncTarget(path="relative"), None)
    v6 = resolve_rsync_endpoint(RsyncTarget(host="fd00::60", user="root", key_ref="file:/k", path="/b"), None)
    assert v6.location("s") == "root@[fd00::60]:/b/s"


# ================================================================================================ rsync/ssh command lines
def test_ssh_rsync_command_lines(tmp_path: Path) -> None:
    key = tmp_path / "backup_ed25519"
    key.write_text("key", encoding="utf-8")
    known = tmp_path / "known_hosts"
    target = RsyncTarget(
        host="192.168.178.60", user="root", key_ref=f"file:{key}", known_hosts=known, path="/mnt/user/backups/hermclaw", bwlimit_kbps=50000
    )
    store = RsyncStore(target, resolve_rsync_endpoint(target, None), env="test")
    ssh = ssh_command(store.endpoint, target, key)
    assert ssh.split() == [
        "ssh",
        "-F",
        "/dev/null",
        "-i",
        str(key),
        "-p",
        "22",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "ConnectTimeout=20",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=4",
        "-o",
        "LogLevel=ERROR",
    ]
    with store._transport() as transport:
        assert transport[:3] == ["rsync", "--timeout=300", "--bwlimit=50000"]
        assert transport[-2:] == ["-e", ssh]
        upload = store.upload_argv(transport, tmp_path / SET, SET, data_only=True)
        verify = store.verify_argv(transport, tmp_path / SET, SET, data_only=False)
        listing = store.list_argv(transport)
        delete = store.delete_argv(transport, tmp_path / "empty", SET)
    remote = f"root@192.168.178.60:/mnt/user/backups/hermclaw/{SET}/"
    assert upload[-3:] == ["--", f"{tmp_path / SET}/", remote]
    for flag in ("-a", "--partial", "--partial-dir=.rsync-partial", "--chmod=D0700,F0600", "--exclude=/MANIFEST.json"):
        assert flag in upload
    for flag in ("--checksum", "--dry-run", "--itemize-changes", "--delete"):
        assert flag in verify
    assert "--exclude=/MANIFEST.json" not in verify and verify[-1] == remote
    assert listing[-1] == "root@192.168.178.60:/mnt/user/backups/hermclaw/" and "--list-only" in listing
    assert delete[-4:] == ["--exclude=*", "--", f"{tmp_path / 'empty'}/", "root@192.168.178.60:/mnt/user/backups/hermclaw/"]
    assert f"--include=/{SET}/***" in delete
    assert "key" not in " ".join(upload).replace(str(key), "")  # only the key path, never key material


def test_ssh_key_refs_inline_secret_is_written_privately_and_removed(tmp_path: Path) -> None:
    with ssh_key_file("literal:-----BEGIN OPENSSH PRIVATE KEY-----", env="test") as path:
        assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
        held = path
    assert not held.exists()
    with pytest.raises(BackupError, match="does not exist"), ssh_key_file(f"file:{tmp_path / 'missing'}"):
        pass
    with pytest.raises(BackupError, match="invalid credential name"), ssh_key_file("cred:../../etc/shadow"):
        pass


def test_itemized_differences_and_listing_parser() -> None:
    output = "\n".join(
        [
            ".d..t...... ./",
            ".f...p..... db.dump",  # attributes only
            ">fc.t...... artifacts.tar.gz",  # content differs
            "cd+++++++++ newdir/",
            "*deleting   stray.txt",
            "",
        ]
    )
    assert itemized_differences(output) == [">fc.t...... artifacts.tar.gz", "cd+++++++++ newdir/", "*deleting   stray.txt"]
    assert itemized_differences(".d..t...... ./\n") == []
    listing = "\n".join(
        [
            "drwx------          4,096 2026/10/14 02:30:00 .",
            f"drwx------          4,096 2026/10/14 02:30:00 {SET}",
            f"-rw-------          2,345 2026/10/14 02:31:00 {SET}/MANIFEST.json",
            "drwx------          4,096 2026/10/13 02:30:00 hermclaw-20261013T023000Z-ffffff",
            "drwx------          4,096 2026/10/13 02:30:00 lost+found",
            "-rw-------          2,345 2026/10/13 02:31:00 lost+found/MANIFEST.json",
        ]
    )
    store_target = RsyncTarget(path="/b")
    sets = parse_listing(listing, resolve_rsync_endpoint(store_target, None))
    assert [(s.set_id, s.complete) for s in sets] == [("hermclaw-20261013T023000Z-ffffff", False), (SET, True)]


# ================================================================================================ retention
def _listing(ts: datetime, *, complete: bool = True, partial: bool = False, rand: str = "aaaaaa") -> SetListing:
    sid = f"hermclaw-{ts:%Y%m%dT%H%M%SZ}-{rand}"
    return SetListing(sid, complete, set_time(sid), f"/t/{sid}", partial_dir=partial)


def test_retention_daily_weekly_monthly_and_protect() -> None:
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    sets = [_listing(now - timedelta(days=d, hours=h)) for d in range(0, 70) for h in (1, 3)]
    plan = plan_retention(sets, RetentionPolicy(keep_last=2, keep_daily=7, keep_weekly=4, keep_monthly=3), now=now)
    kept = {s.set_id for s in plan.keep}
    newest_per_day = {s.set_id for s in sets if s.created_at.hour == 11}  # h=1 is the newer one of each day
    assert sets[0].set_id in kept and sets[1].set_id in kept  # keep_last=2 (both of today)
    daily = [sid for sid, why in plan.reasons.items() if any(r.startswith("daily") for r in why)]
    assert len(daily) == 7 and set(daily) <= newest_per_day
    assert sum(1 for why in plan.reasons.values() if any(r.startswith("weekly") for r in why)) == 4
    assert sum(1 for why in plan.reasons.values() if any(r.startswith("monthly") for r in why)) == 3
    assert len(plan.keep) + len(plan.delete) == len(sets)
    oldest = sets[-1]
    assert oldest in plan.delete
    protected = plan_retention(sets, RetentionPolicy(keep_last=1, keep_daily=0, keep_weekly=0), now=now, protect=[oldest.set_id])
    assert oldest.set_id in {s.set_id for s in protected.keep} and protected.reasons[oldest.set_id] == ["protected"]


def test_retention_incomplete_sets() -> None:
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    complete = _listing(now - timedelta(hours=5))
    young = _listing(now - timedelta(hours=1), complete=False, rand="bbbbbb")
    old = _listing(now - timedelta(days=3), complete=False, rand="cccccc")
    twin = SetListing(complete.set_id, False, complete.created_at, "/t/x.partial", partial_dir=True)
    plan = plan_retention([complete, young, old, twin], RetentionPolicy(keep_last=1, stale_partial_hours=4), now=now)
    assert {s.set_id for s in plan.keep} == {complete.set_id}
    assert young not in plan.delete and old in plan.delete and twin in plan.delete
    assert plan.reasons[f"{complete.set_id}.partial"] == ["stale leftover of a complete set"]


# ================================================================================================ archives
@pytest.mark.parametrize(
    ("rel", "is_dir", "excluded"),
    [
        ("secrets", True, True),
        ("a/b/secrets", True, True),
        ("secrets", False, False),  # a file named "secrets" is not the directory pattern
        ("tls/server.KEY", False, True),
        ("tls/server.pem", False, True),
        ("ssh/id_ed25519", False, True),
        ("ssh/backup_ed25519", False, True),
        ("ssh/backup_ed25519.pub", False, False),
        ("hosts.yaml", False, False),
        (".pgpass", False, True),
        ("editor.yaml.swp", False, True),
    ],
)
def test_always_exclude_patterns(rel: str, is_dir: bool, excluded: bool) -> None:
    from pathlib import PurePosixPath

    assert is_excluded(PurePosixPath(rel), is_dir=is_dir, patterns=ALWAYS_EXCLUDE) is excluded


def test_plaintext_secret_detection() -> None:
    assert plaintext_secret_reason(b"password: hunter2hunter2\n") == "plaintext secret"
    assert plaintext_secret_reason(b"url: postgresql://u:pw123@db/x\n") == "plaintext secret"
    assert plaintext_secret_reason(b"token: glpat-abcdefghijklmnopqrstu\n") == "plaintext secret"
    assert plaintext_secret_reason(b"-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n") == "private key"
    for harmless in (
        b"password: cred:hermclaw-db-password\n",
        b"token: file:/etc/hermclaw/secrets/x\n",
        b"api_key: env:LITELLM_KEY\n",
        b"password: ${DB_PASSWORD}\n",
        b"secret: {{ vault_secret }}\n",
        b"api_token_ref: cred:api-admin-token\n",
        b"max_tokens: 4096\n",
        b"password: null\n",
        b"\x00\x01binary password: hunter2hunter2",
    ):
        assert plaintext_secret_reason(harmless) is None, harmless


def test_member_name_problems() -> None:
    assert member_name_problem("config/hosts.yaml") is None
    assert "absolute" in (member_name_problem("/etc/passwd") or "")
    assert "escapes" in (member_name_problem("config/../../x") or "")
    assert member_name_problem("") is not None and member_name_problem("a\x00b") is not None


def test_tarball_roundtrip_and_inspection(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "a.txt").write_text("a")
    (src / "id_rsa").write_text("x")
    (src / "pasted").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    (src / "link").symlink_to("/etc/hostname")
    rec = tmp_path / "records"
    rec.mkdir()
    (rec / "t.jsonl").write_text('{"a": 1}\n{"a": 2}\n')
    dest = tmp_path / "out.tar.gz"
    stats = build_tarball(dest, [ArchiveSource("config", src, excludes=ALWAYS_EXCLUDE), ArchiveSource("records", rec)])
    assert dest.stat().st_mode & 0o777 == 0o600
    assert stats.files == 2 and stats.per_root_files == {"config": 1, "records": 1}
    assert any("id_rsa" in e for e in stats.excluded) and any("pasted (private key)" in e for e in stats.excluded)
    assert any("link (symlink)" in e for e in stats.skipped)
    check = inspect_tarball(dest)
    assert check.ok and check.files == 2 and check.jsonl_lines == {"records/t.jsonl": 2}
    assert extract_tarball(dest, tmp_path / "x") == 2 and (tmp_path / "x" / "config" / "sub" / "a.txt").read_text() == "a"
    with pytest.raises(FileExistsError):  # never overwrites
        build_tarball(dest, [])
    bad_jsonl = tmp_path / "bad.tar.gz"
    with tarfile.open(bad_jsonl, "w:gz") as tar:
        info = tarfile.TarInfo("records/x.jsonl")
        info.size = 8
        tar.addfile(info, io.BytesIO(b"{oops}\n\n"))
    assert not inspect_tarball(bad_jsonl).ok


# ================================================================================================ manifest
def test_set_ids_and_signature() -> None:
    sid = new_set_id(datetime(2026, 10, 14, 2, 30, 0, 250_999, tzinfo=UTC))
    assert is_set_id(sid) and sid.startswith("hermclaw-20261014T023000250Z-")
    assert set_time(sid) == datetime(2026, 10, 14, 2, 30, 0, 250_000, tzinfo=UTC)
    assert set_time(SET) == datetime(2026, 10, 14, 2, 30, tzinfo=UTC)  # second-precision ids stay valid
    # sets made within the same second sort by time, not by their random suffix
    early = new_set_id(datetime(2026, 10, 14, 2, 30, 0, 100_000, tzinfo=UTC))
    late = new_set_id(datetime(2026, 10, 14, 2, 30, 0, 900_000, tzinfo=UTC))
    assert set_time(early) < set_time(late)
    for bad in (
        "../hermclaw-20261014T023000Z-aaaaaa",
        "hermclaw-20261014T023000Z-AAAAAA",
        "hermclaw-20261014T0230001Z-aaaaaa",
        "x",
        f"{sid}/..",
    ):
        assert not is_set_id(bad)
        with pytest.raises(BackupError):
            check_set_id(bad)
    assert sign(b"m", b"k") != sign(b"m2", b"k") and len(sign(b"m", b"k")) == 64


# ================================================================================================ libpq
def test_pgconn_from_url_and_credentials(tmp_path: Path) -> None:
    conn = PgConn.from_url("postgresql+psycopg://hermclaw:s3cr:et\\x@db.lan:5433/hermclaw?sslmode=require")
    assert (conn.host, conn.port, conn.user, conn.dbname) == ("db.lan", 5433, "hermclaw", "hermclaw")
    assert "s3cr" not in repr(conn) and "s3cr" not in conn.display()
    env = conn.libpq_env()
    assert env == {"PGDATABASE": "hermclaw", "PGHOST": "db.lan", "PGPORT": "5433", "PGUSER": "hermclaw", "PGSSLMODE": "require"}
    with pg_tool_env(conn) as tool_env:
        passfile = Path(tool_env["PGPASSFILE"])
        assert passfile.stat().st_mode & 0o777 == 0o600
        assert passfile.read_text() == "*:*:*:*:s3cr\\:et\\\\x\n"
        assert "PGPASSWORD" not in tool_env
    assert not passfile.exists() and not passfile.parent.exists()
    sock = PgConn.from_url("postgresql+psycopg://hermclaw@/hermclaw?host=/run/postgresql")
    assert sock.host == "/run/postgresql" and sock.password is None
    with pg_tool_env(sock) as tool_env:
        assert "PGPASSFILE" not in tool_env
    for bad in ("mysql://x@y/z", "postgresql://x@y/", "postgresql://x@y/db?sslpassword=x"):
        with pytest.raises(BackupError):
            PgConn.from_url(bad)
    with pytest.raises(BackupError, match="line breaks"), pg_tool_env(conn.with_password("a\nb")):
        pass


def test_filter_toc_comments_out_extensions() -> None:
    listing = "\n".join(
        [
            ";",
            "; Archive created at 2026-10-14",
            "2; 3079 16385 EXTENSION - vector ",
            "4562; 0 0 COMMENT - EXTENSION vector ",
            "215; 1259 16400 TABLE public jobs hermclaw",
            "4400; 0 16400 TABLE DATA public jobs hermclaw",
        ]
    )
    toc = filter_toc(listing)
    assert toc.extensions == ["vector"] and len(toc.skipped) == 2
    lines = toc.listing.splitlines()
    assert lines[2].startswith(";2; 3079") and lines[3].startswith(";4562;") and lines[4].startswith("215;")


def test_find_pg_tool_prefers_matching_major(tmp_path: Path) -> None:
    for major in ("14", "16"):
        (tmp_path / major / "bin").mkdir(parents=True)
        (tmp_path / major / "bin" / "pg_dump").write_text("")
    assert find_pg_tool("pg_dump", lib_dir=tmp_path) == str(tmp_path / "16" / "bin" / "pg_dump")
    assert find_pg_tool("pg_dump", lib_dir=tmp_path, min_major=15) == str(tmp_path / "16" / "bin" / "pg_dump")
    with pytest.raises(BackupError, match="not found in pg_bin_dir"):
        find_pg_tool("pg_restore", bin_dir=tmp_path / "14" / "bin")
    with pytest.raises(ValueError):
        find_pg_tool("rm", lib_dir=tmp_path)


# ================================================================================================ misc
def test_referenced_secret_files(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("x:\n  key_ref: file:/etc/hermclaw/ssh/id_ed25519\n  t: 'file:/etc/hermclaw/tok'\n")
    (tmp_path / "b.txt").write_text("file:/not/scanned\n")
    assert referenced_secret_files(tmp_path) == {Path("/etc/hermclaw/ssh/id_ed25519"), Path("/etc/hermclaw/tok")}
    assert referenced_secret_files(tmp_path / "missing") == set()


def test_event_payload_flattening_avoids_logrecord_collisions() -> None:
    flat = _flat({"name": "x", "msg": "y", "rows": 3, "nested": {"a": 1}, "list": [1]})
    assert flat == {"p_name": "x", "p_msg": "y", "rows": 3}


async def test_lock_conflict_and_release(tmp_path: Path) -> None:
    lock = tmp_path / "l" / ".lock"
    async with backup_lock(lock, wait_seconds=0, what="one"):
        with pytest.raises(BackupError) as err:
            async with backup_lock(lock, wait_seconds=0.3, what="two"):
                pass
        assert err.value.code == "BACKUP_LOCKED" and "op=one" in err.value.message
    async with backup_lock(lock, wait_seconds=0, what="three"):
        assert "op=three" in lock.read_text()
    assert lock.read_text() == ""


def test_cli_set_argument() -> None:
    assert _set_arg("latest", False) == "latest" and _set_arg(SET, True) == "latest"
    assert _set_arg(SET, False) == SET
    assert isinstance(_set_arg("./x/" + SET, False), Path)


def test_backup_config_paths_default_from_settings(tmp_path: Path) -> None:
    from hermclaw.core.settings import Settings

    settings = Settings(data_dir=tmp_path / "data", config_dir=tmp_path / "cfg")
    cfg = BackupConfig(target=LocalTarget(path=tmp_path))
    assert cfg.effective_staging_dir(settings) == tmp_path / "data" / "backup" / "staging"
    assert cfg.effective_artifacts_dir(settings) == tmp_path / "data" / "artifacts"
    assert cfg.effective_config_dir(settings) == tmp_path / "cfg"
    assert cfg.lock_path(settings) == tmp_path / "data" / "backup" / "staging" / ".hermclaw-backup.lock"
