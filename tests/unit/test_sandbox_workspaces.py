"""P18 workspace sync: safe tar extraction (attack archives), tar building, manifests and diffs."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
from pathlib import Path

import pytest

from worker.execution.workspaces import (
    UnsafeArchiveError,
    WorkspaceLimitExceeded,
    build_tar,
    diff_manifest,
    extract_tar_safely,
    manifest,
    normalize_member_path,
)


def _tar(*members: tuple[str, str, bytes | str], gz: bool = False) -> bytes:
    """members: (kind, name, data-or-linkname) with kind in file|dir|sym|lnk|fifo|chr|exec|suid."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if gz else "w", format=tarfile.PAX_FORMAT) as tf:
        for kind, name, payload in members:
            info = tarfile.TarInfo(name)
            info.mtime = 1_700_000_000
            if kind in ("file", "exec", "suid"):
                data = payload if isinstance(payload, bytes) else payload.encode()
                info.size = len(data)
                info.mode = {"file": 0o644, "exec": 0o755, "suid": 0o4777}[kind]
                tf.addfile(info, io.BytesIO(data))
                continue
            info.type = {
                "dir": tarfile.DIRTYPE,
                "sym": tarfile.SYMTYPE,
                "lnk": tarfile.LNKTYPE,
                "fifo": tarfile.FIFOTYPE,
                "chr": tarfile.CHRTYPE,
            }[kind]
            if kind in ("sym", "lnk"):
                info.linkname = payload if isinstance(payload, str) else payload.decode()
            info.mode = 0o755
            tf.addfile(info)
    return buf.getvalue()


def _read(data: bytes) -> dict[str, tuple[str, bytes | str]]:
    out: dict[str, tuple[str, bytes | str]] = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        for m in tf.getmembers():
            if m.isreg():
                f = tf.extractfile(m)
                assert f is not None
                out[m.name] = ("file", f.read())
            elif m.issym():
                out[m.name] = ("sym", m.linkname)
            elif m.isdir():
                out[m.name] = ("dir", "")
    return out


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------------------------- extraction
def test_extracts_files_dirs_links_and_modes(dest: Path) -> None:
    data = _tar(
        ("dir", "./", b""),
        ("dir", "src", b""),
        ("file", "src/app.py", b"print('hi')\n"),
        ("exec", "run.sh", b"#!/bin/sh\n"),
        ("suid", "danger", b"x"),
        ("sym", "src/link.py", "app.py"),
        ("sym", "docs", "src"),
        ("lnk", "copy.py", "src/app.py"),
        ("file", "./a//b/./c.txt", b"nested"),
    )
    extract_tar_safely(data, dest)
    assert (dest / "src/app.py").read_bytes() == b"print('hi')\n"
    assert (dest / "a/b/c.txt").read_bytes() == b"nested"
    assert os.readlink(dest / "src/link.py") == "app.py" and os.readlink(dest / "docs") == "src"
    assert (dest / "copy.py").read_bytes() == b"print('hi')\n"
    assert not (dest / "copy.py").samefile(dest / "src/app.py")  # hardlinks are materialised as copies
    assert stat.S_IMODE((dest / "run.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((dest / "src/app.py").stat().st_mode) == 0o644
    assert stat.S_IMODE((dest / "danger").stat().st_mode) == 0o755  # no setuid, no group/other write
    assert int((dest / "src/app.py").stat().st_mtime) == 1_700_000_000


def test_gzip_archives_and_creates_missing_dest(tmp_path: Path) -> None:
    target = tmp_path / "new" / "ws"
    extract_tar_safely(_tar(("file", "x.txt", b"1"), gz=True), target)
    assert (target / "x.txt").read_text() == "1"


@pytest.mark.parametrize(
    "members",
    [
        [("file", "/etc/evil", b"x")],
        [("file", "../evil", b"x")],
        [("file", "a/../../evil", b"x")],
        [("file", "a/../b", b"x")],
        [("sym", "l", "/etc/passwd")],
        [("sym", "l", "../outside")],
        [("sym", "a/l", "../../outside")],
        [("lnk", "h", "/etc/passwd")],
        [("lnk", "h", "../outside")],
        [("lnk", "h", "not-in-archive")],
        [("fifo", "pipe", b"")],
        [("chr", "dev", b"")],
        [("file", "x", b"1"), ("file", "x", b"2")],
        [("file", "a", b"1"), ("file", "a/b", b"2")],
        [("sym", "a", "sub"), ("file", "a/b", b"write-through")],
        [("file", "", b"x")],
        [("file", "x" * 300, b"x")],
    ],
)
def test_unsafe_archives_rejected_before_any_write(dest: Path, members: list[tuple[str, str, bytes | str]]) -> None:
    sentinel = dest.parent / "outside"
    with pytest.raises(UnsafeArchiveError) as exc:
        extract_tar_safely(_tar(*members), dest)
    assert exc.value.code == "WORKSPACE_ARCHIVE_UNSAFE"
    assert list(dest.iterdir()) == []  # validated completely before the first write
    assert not sentinel.exists() and not Path("/etc/evil").exists()


def test_symlink_chain_escape_detected_and_removed(dest: Path) -> None:
    # each link is harmless on its own (lexically inside), the chain resolves to dest/..
    data = _tar(("sym", "s1", "."), ("sym", "s2", "s1/.."))
    with pytest.raises(UnsafeArchiveError, match="outside the workspace"):
        extract_tar_safely(data, dest)
    assert not os.path.lexists(dest / "s2")
    assert os.readlink(dest / "s1") == "."


def test_existing_symlink_in_dest_is_never_written_through(dest: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (dest / "escape").symlink_to(outside)  # e.g. created by a sandbox command in merge mode
    with pytest.raises(UnsafeArchiveError, match="symlink"):
        extract_tar_safely(_tar(("file", "escape/pwned", b"x")), dest)
    assert list(outside.iterdir()) == []
    # a symlink member pointing through it resolves outside -> rejected and removed
    with pytest.raises(UnsafeArchiveError, match="outside"):
        extract_tar_safely(_tar(("sym", "inner", "escape/x")), dest)
    assert not os.path.lexists(dest / "inner")


def test_existing_file_symlink_is_replaced_not_followed(dest: Path, tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("original")
    (dest / "config.txt").symlink_to(victim)
    extract_tar_safely(_tar(("file", "config.txt", b"new")), dest)
    assert victim.read_text() == "original"
    assert not (dest / "config.txt").is_symlink() and (dest / "config.txt").read_text() == "new"


def test_merge_replaces_files_and_keeps_others(dest: Path) -> None:
    (dest / "keep.txt").write_text("keep")
    (dest / "a.txt").write_text("old")
    (dest / "d").mkdir()
    extract_tar_safely(_tar(("file", "a.txt", b"new"), ("dir", "d", b""), ("file", "d/x", b"1")), dest)
    assert (dest / "keep.txt").read_text() == "keep" and (dest / "a.txt").read_text() == "new" and (dest / "d/x").read_text() == "1"
    with pytest.raises(UnsafeArchiveError, match="directory"):
        extract_tar_safely(_tar(("file", "d", b"file over dir")), dest)
    with pytest.raises(UnsafeArchiveError):
        extract_tar_safely(_tar(("dir", "a.txt", b"")), dest)


def test_size_and_count_limits(dest: Path) -> None:
    with pytest.raises(WorkspaceLimitExceeded) as exc:
        extract_tar_safely(_tar(("file", "big", b"x" * 2000)), dest, max_file_bytes=1000)
    assert exc.value.code == "WORKSPACE_LIMIT_EXCEEDED"
    with pytest.raises(WorkspaceLimitExceeded):
        extract_tar_safely(_tar(("file", "a", b"x" * 600), ("file", "b", b"x" * 600)), dest, max_total_bytes=1000)
    with pytest.raises(WorkspaceLimitExceeded):  # hardlink copies count towards the total
        extract_tar_safely(_tar(("file", "a", b"x" * 600), ("lnk", "b", "a")), dest, max_total_bytes=1000)
    with pytest.raises(WorkspaceLimitExceeded):
        extract_tar_safely(_tar(*[("file", f"f{i}", b"") for i in range(11)]), dest, max_members=10)
    assert list(dest.iterdir()) == []


def test_corrupt_archives(dest: Path) -> None:
    for data in (b"", b"not a tar at all" * 100, _tar(("file", "x", b"y" * 5000))[:700], _tar(("file", "x", b"1"), gz=True)[:30]):
        with pytest.raises(UnsafeArchiveError):
            extract_tar_safely(data, dest)


def test_destination_symlink_rejected(tmp_path: Path, dest: Path) -> None:
    link = tmp_path / "wslink"
    link.symlink_to(dest)
    with pytest.raises(UnsafeArchiveError):
        extract_tar_safely(_tar(("file", "x", b"1")), link)


def test_normalize_member_path() -> None:
    assert normalize_member_path("./a//b/./c") == "a/b/c"
    for bad in ("", "/a", "a/../b", "..", ".", "./", "a\x00b"):
        with pytest.raises(UnsafeArchiveError):
            normalize_member_path(bad)


# ---------------------------------------------------------------------------------------------- build / manifest
@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    (root / "src/pkg").mkdir(parents=True)
    (root / "src/pkg/mod.py").write_text("x = 1\n")
    (root / "README.md").write_text("# hi\n")
    (root / "empty").mkdir()
    (root / "run.sh").write_text("#!/bin/sh\n")
    (root / "run.sh").chmod(0o755)
    (root / ".git/objects").mkdir(parents=True)
    (root / ".git/config").write_text("[core]\n")
    (root / ".gitignore").write_text("*.pyc\n")
    secret = tmp_path / "host-secret.txt"
    secret.write_text("HOST SECRET")
    (root / "leak").symlink_to(secret)  # created by a sandbox command
    (root / "etc").symlink_to("/etc")
    os.mkfifo(root / "pipe")
    return root


def test_manifest_hashes_files_and_links_without_following(tree: Path) -> None:
    m = manifest(tree)
    assert m["README.md"] == hashlib.sha256(b"# hi\n").hexdigest()
    assert m["src/pkg/mod.py"] == hashlib.sha256(b"x = 1\n").hexdigest()
    assert ".gitignore" in m and not any(p.startswith(".git/") for p in m)
    assert m["leak"] == hashlib.sha256(b"symlink\x00" + os.fsencode(os.readlink(tree / "leak"))).hexdigest()
    assert m["leak"] != hashlib.sha256(b"HOST SECRET").hexdigest()
    assert "etc" in m and not any(p.startswith("etc/") for p in m)
    assert "pipe" not in m and "empty" not in m  # FIFOs never opened, directories not listed
    assert list(m) == sorted(m)
    assert manifest(tree, exclude=())[".git/config"] == hashlib.sha256(b"[core]\n").hexdigest()


def test_build_tar_round_trip(tree: Path, tmp_path: Path) -> None:
    data = build_tar(tree)
    content = _read(data)
    assert content["README.md"] == ("file", b"# hi\n")
    assert content["leak"][0] == "sym" and b"HOST SECRET" not in data
    assert content["etc"] == ("sym", "/etc")
    assert "empty" in content and "pipe" not in content
    assert not any(name == ".git" or name.startswith(".git/") for name in content)
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        for m in tf.getmembers():
            assert (m.uid, m.gid, m.uname, m.gname) == (0, 0, "", "")
        assert stat.S_IMODE(tf.getmember("run.sh").mode) == 0o755
    # the archive of a workspace with absolute symlinks is (correctly) refused by the safe extractor ...
    with pytest.raises(UnsafeArchiveError, match="absolute symlink"):
        extract_tar_safely(data, tmp_path / "refused")
    # ... a workspace without them round-trips with an identical manifest
    os.unlink(tree / "etc")
    os.unlink(tree / "leak")
    (tree / "rel").symlink_to("src/pkg/mod.py")
    copy = tmp_path / "copy"
    extract_tar_safely(build_tar(tree), copy)
    assert manifest(copy) == manifest(tree)


def test_build_tar_selected_paths(tree: Path) -> None:
    content = _read(build_tar(tree, ["src", "README.md", "src/pkg/mod.py", "leak"]))
    assert set(content) == {"src", "src/pkg", "src/pkg/mod.py", "README.md", "leak"}
    with pytest.raises(FileNotFoundError):
        build_tar(tree, ["missing.txt"])
    with pytest.raises(FileNotFoundError):
        build_tar(tree, ["nodir/x"])
    for bad in ("../outside", "/etc/passwd", "a/../../b", ""):
        with pytest.raises(ValueError):
            build_tar(tree, [bad])
    with pytest.raises(ValueError, match="symlink"):
        build_tar(tree, ["etc/passwd"])  # never traverses a symlinked directory
    with pytest.raises(ValueError, match="excluded"):
        build_tar(tree, [".git/config"])
    assert "pipe" not in _read(build_tar(tree, ["pipe"]))


def test_build_tar_limits_and_gzip(tree: Path) -> None:
    with pytest.raises(WorkspaceLimitExceeded):
        build_tar(tree, max_total_bytes=5)
    gz = build_tar(tree, ["README.md"], compress=True)
    assert gz[:2] == b"\x1f\x8b" and _read(gz)["README.md"] == ("file", b"# hi\n")


def test_build_tar_and_manifest_reject_symlinked_root(tree: Path, tmp_path: Path) -> None:
    link = tmp_path / "rootlink"
    link.symlink_to(tree)
    with pytest.raises(ValueError):
        build_tar(link)
    with pytest.raises(ValueError):
        manifest(link)


def test_diff_manifest() -> None:
    old = {"a": "1", "b": "2", "c": "3"}
    new = {"a": "1", "b": "20", "d": "4"}
    assert diff_manifest(old, new) == (["b", "d"], ["c"])
    assert diff_manifest({}, {}) == ([], [])
    assert diff_manifest(old, old) == ([], [])


def test_manifest_diff_after_changes(tree: Path) -> None:
    before = manifest(tree)
    (tree / "README.md").write_text("changed\n")
    (tree / "src/new.py").write_text("")
    (tree / "src/pkg/mod.py").unlink()
    (tree / "leak").unlink()
    (tree / "leak").symlink_to("README.md")  # retargeted link counts as changed
    changed, deleted = diff_manifest(before, manifest(tree))
    assert changed == ["README.md", "leak", "src/new.py"] and deleted == ["src/pkg/mod.py"]
