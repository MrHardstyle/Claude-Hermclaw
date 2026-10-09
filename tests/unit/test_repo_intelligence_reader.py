"""P11 11.11: targeted reads (line ranges, character budget, binary/size limits, workspace confinement) + redaction."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermclaw.core.errors import NotFoundError, PolicyViolation, ValidationFailed
from hermclaw.repo_intelligence import FileReader, RepoIntelConfig
from hermclaw.repo_intelligence.paths import is_test_path, matches_glob, normalize_rel, resolve_in_root
from hermclaw.repo_intelligence.redact import redact_code


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "ws"
    (r / "src").mkdir(parents=True)
    (r / "src/app.py").write_text("".join(f"line {i}\n" for i in range(1, 101)), encoding="utf-8")
    (r / "src/crlf.txt").write_bytes(b"a\r\nb\r\nc")
    (r / "src/bom.py").write_bytes(b"\xef\xbb\xbfprint('x')\n")
    (r / "src/one_long_line.min.js").write_text("x" * 5000, encoding="utf-8")
    (r / "image.bin").write_bytes(b"\x00\x01\x02binary")
    (r / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (r / ".git").mkdir()
    (r / ".git/config").write_text("[core]\n", encoding="utf-8")
    return r


async def test_line_ranges_and_numbering(root: Path) -> None:
    reader = FileReader()
    res = await reader.read(root, "src/app.py", 10, 12)
    assert (res.start_line, res.end_line, res.total_lines, res.truncated) == (10, 12, 100, False)
    assert res.text == "line 10\nline 11\nline 12\n"
    assert res.numbered() == "10 | line 10\n11 | line 11\n12 | line 12\n"
    tail = await reader.read(root, "./src/app.py", 99)
    assert tail.text == "line 99\nline 100\n" and tail.end_line == 100
    beyond = await reader.read(root, "src/app.py", 500, 600)
    assert beyond.text == "" and beyond.end_line == 499 and not beyond.truncated
    inverted = await reader.read(root, "src/app.py", 20, 10)
    assert inverted.text == ""
    crlf = await reader.read(root, "src/crlf.txt")
    assert crlf.text == "a\r\nb\r\nc" and crlf.total_lines == 3  # exact bytes for patching
    bom = await reader.read(root, "src/bom.py")
    assert bom.text == "print('x')\n"


async def test_character_budget_cuts_at_line_boundaries(root: Path) -> None:
    reader = FileReader()
    res = await reader.read(root, "src/app.py", 1, 100, max_chars=30)
    assert res.text == "line 1\nline 2\nline 3\nline 4\n" and res.truncated and res.end_line == 4
    long = await reader.read(root, "src/one_long_line.min.js", max_chars=100)
    assert len(long.text) == 100 and long.truncated and long.end_line == 1
    default = await FileReader(RepoIntelConfig(read_default_max_chars=50)).read(root, "src/app.py")
    assert len(default.text) <= 50 and default.truncated


async def test_refusals(root: Path, tmp_path: Path) -> None:
    reader = FileReader()
    with pytest.raises(ValidationFailed) as binary:
        await reader.read(root, "image.bin")
    assert binary.value.code == "REPO_FILE_BINARY"
    with pytest.raises(ValidationFailed) as big:
        await FileReader(RepoIntelConfig(max_read_file_bytes=10)).read(root, "src/app.py")
    assert big.value.code == "REPO_FILE_TOO_LARGE"
    with pytest.raises(ValidationFailed):
        await reader.read(root, "src")  # a directory
    with pytest.raises(NotFoundError):
        await reader.read(root, "src/missing.py")
    for forbidden in ("../outside.txt", "/etc/passwd", "src/../../x", ".git/config", ".env", "C:/windows/win.ini", "a\x00b"):
        with pytest.raises((PolicyViolation, ValidationFailed)):
            await reader.read(root, forbidden)
    # symlinks: escaping the workspace or pointing at protected files is refused, inside links are followed
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    os.symlink(outside, root / "escape.txt")
    os.symlink(root / ".env", root / "env_link")
    os.symlink(root / "src/app.py", root / "inner_link.py")
    with pytest.raises(PolicyViolation):
        await reader.read(root, "escape.txt")
    with pytest.raises(PolicyViolation):
        await reader.read(root, "env_link")
    assert (await reader.read(root, "inner_link.py", 1, 1)).text == "line 1\n"


def test_path_helpers() -> None:
    assert normalize_rel("./a//b/./c.py") == "a/b/c.py" and normalize_rel("a\\b.py") == "a/b.py"
    for bad in ("", "  ", "../x", "/abs", "a/../../b", "x\ny"):
        with pytest.raises((PolicyViolation, ValidationFailed)):
            normalize_rel(bad)
    assert matches_glob("deep/dir/id_rsa", "**/id_rsa*") and matches_glob("x.pem", "**/*.pem")
    assert matches_glob("src/a.py", "*.py") and matches_glob("src/a.py", "src/**") and not matches_glob("lib/a.py", "src/**")
    assert matches_glob("a/node_modules/x.js", "**/node_modules/**") and matches_glob("file[1].txt", "file[0-9].txt") is False
    assert is_test_path("tests/unit/test_x.py") and is_test_path("src/x.test.ts") and is_test_path("pkg/x_test.go")
    assert is_test_path("tests/UserTest.php") and not is_test_path("src/contest.py")


def test_resolve_in_root_reports_normalised_path(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a/b.txt").write_text("x", encoding="utf-8")
    norm, real = resolve_in_root(tmp_path, "./a/b.txt")
    assert norm == "a/b.txt" and real == (tmp_path / "a/b.txt").resolve()


def test_code_redaction() -> None:
    src = (
        "DB_PASSWORD = 'hunter2-very-secret'\n"
        '"apiToken": "abcd1234xyz",\n'
        "$client_secret => 'zzzz9999';\n"
        "password = request.form['password']\n"
        "token_count = 5\n"
        "name = 'visible'\n"
        "Authorization: Bearer abcdefghijklmnop\n"
    )
    out = redact_code(src)
    for secret in ("hunter2-very-secret", "abcd1234xyz", "zzzz9999", "abcdefghijklmnop"):
        assert secret not in out
    assert "token_count = 5" in out and "name = 'visible'" in out
    assert redact_code("") == ""


async def test_scope_policy_forbidden_globs_are_protected(root: Path) -> None:
    from hermclaw.core.config import load_config

    cfg = RepoIntelConfig.from_hermclaw(load_config(None), read_default_max_chars=5_000)
    assert "**/secrets/**" in cfg.sensitive_globs and cfg.read_default_max_chars == 5_000
    (root / "secrets").mkdir()
    (root / "secrets/token.txt").write_text("t0ken\n", encoding="utf-8")
    with pytest.raises(PolicyViolation):
        await FileReader(cfg).read(root, "secrets/token.txt")
    assert RepoIntelConfig.from_hermclaw(object()).sensitive_globs == RepoIntelConfig().sensitive_globs
