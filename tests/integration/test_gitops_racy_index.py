"""BUG-059: the scratch index of read_diff/changed_files keeps the real index's mtime (racy-git detection).

A same-size edit in the same second as the index write is only detectable because git treats such entries as
"racily clean" (entry mtime >= index mtime) and compares content. A scratch copy with a fresh mtime lost that and
the change vanished from diffs, changed-file lists, the verifier and the push decision."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from hermclaw.gitops import _fs
from hermclaw.gitops.ops import changed_files, read_diff
from hermclaw.gitops.runner import GitRunner


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=env).stdout.strip()


def test_copy_keeps_the_index_mtime(tmp_path: Path) -> None:
    src, dst = tmp_path / "index", tmp_path / "copy"
    src.write_bytes(b"DIRC")
    os.utime(src, (1_700_000_000, 1_700_000_000))
    assert _fs.copy_if_exists(src, dst)
    assert int(dst.stat().st_mtime) == 1_700_000_000


async def test_same_second_same_size_edit_is_in_the_diff(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    while time.time() % 1 > 0.2:  # start at the beginning of a second: commit and edit share it
        time.sleep(0.01)
    (repo / "module.py").write_text("VALUE = 1\n")
    _git(repo, "add", "module.py")
    _git(repo, "commit", "-q", "-m", "init")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "module.py").write_text("VALUE = 3\n")  # same size, same second as the index write
    time.sleep(1.2)  # the scratch copy is made in a later second
    runner = GitRunner(author_name="t", author_email="t@x")
    assert await changed_files(runner, repo, base) == ["module.py"]
    diff = await read_diff(runner, repo, base)
    assert "+VALUE = 3" in diff.patch
