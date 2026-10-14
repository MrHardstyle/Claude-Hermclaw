"""Verification of a backup set directory (P33 33.4/33.6).

Quick (``deep=False``): manifest schema + set id, optional HMAC signature, every file present with the recorded
size and sha256. Deep (default) additionally reads every tarball to its end (gzip CRC, member safety: relative,
``..``-free, regular files/dirs only), compares file counts per top-level directory and the line counts of the
``records/*.jsonl`` exports with the manifest, and lists the dump's catalog with ``pg_restore --list``.

``pg_restore --list`` is only a catalog check (research 20261008-022, PostgreSQL BUG #16458) – the real proof is the
restore test (``hermclaw.backup.restore.restore_test``), which builds on this verification.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from hermclaw.backup import runner
from hermclaw.backup.archive import TarCheck, inspect_tarball
from hermclaw.backup.errors import BackupError
from hermclaw.backup.manifest import (
    ARTIFACTS_TAR,
    CONFIG_TAR,
    DB_DUMP,
    MANIFEST,
    MANIFEST_SIG,
    RESTORE_REPORT,
    ArchiveInfo,
    Manifest,
    read_manifest,
    sha256_file,
)
from hermclaw.backup.pg import filter_toc, find_pg_tool

ALLOWED_EXTRA = frozenset({MANIFEST, MANIFEST_SIG, RESTORE_REPORT})
TOC_TIMEOUT_SECONDS = 600


class FileCheck(BaseModel):
    name: str
    ok: bool
    expected_size: int
    actual_size: int | None = None
    expected_sha256: str
    actual_sha256: str | None = None
    problem: str | None = None


class ArchiveCheck(BaseModel):
    name: str
    ok: bool
    files: int = 0
    dirs: int = 0
    bytes: int = 0
    root_files: dict[str, int] = Field(default_factory=dict)
    jsonl_lines: dict[str, int] = Field(default_factory=dict)
    problems: list[str] = Field(default_factory=list)


class DumpCheck(BaseModel):
    ok: bool
    toc_entries: int = 0
    table_data_entries: int = 0
    extensions: list[str] = Field(default_factory=list)
    problem: str | None = None


class VerifyReport(BaseModel):
    set_id: str
    ok: bool
    deep: bool
    location: str
    signature: str = "unsigned"  # valid | unsigned | unverified (signature present, no key configured)
    files: list[FileCheck] = Field(default_factory=list)
    archives: list[ArchiveCheck] = Field(default_factory=list)
    dump: DumpCheck | None = None
    unexpected_files: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)

    def failed(self, name: str) -> bool:
        """True if the file ``name`` failed its checksum/size check (or is missing)."""
        return any(f.name == name and not f.ok for f in self.files)


def _check_file(directory: Path, name: str, expected_sha: str, expected_size: int) -> FileCheck:
    path = directory / name
    check = FileCheck(name=name, ok=False, expected_size=expected_size, expected_sha256=expected_sha)
    if path.is_symlink() or not path.is_file():
        check.problem = "missing (or not a regular file)"
        return check
    digest, size = sha256_file(path)
    check.actual_sha256, check.actual_size = digest, size
    if size != expected_size:
        check.problem = f"size {size} != manifest {expected_size}"
    elif digest != expected_sha:
        check.problem = "sha256 mismatch (file modified or corrupted)"
    else:
        check.ok = True
    return check


def _archive_check(name: str, tar: TarCheck, info: ArchiveInfo, manifest: Manifest) -> ArchiveCheck:
    out = ArchiveCheck(
        name=name,
        ok=tar.ok,
        files=tar.files,
        dirs=tar.dirs,
        bytes=tar.bytes,
        root_files=tar.root_files,
        jsonl_lines=tar.jsonl_lines,
        problems=list(tar.problems),
    )
    if tar.files != info.files:
        out.problems.append(f"{tar.files} files in the archive, manifest says {info.files}")
    expected_roots = {k: v for k, v in info.root_files.items() if v}
    if tar.root_files != expected_roots:
        out.problems.append(f"files per directory {tar.root_files} != manifest {expected_roots}")
    if name == ARTIFACTS_TAR:
        for record in manifest.records:
            lines = tar.jsonl_lines.get(record.member)
            if lines != record.rows:
                out.problems.append(f"{record.member}: {lines} lines, manifest says {record.rows} rows")
    out.ok = not out.problems
    return out


def _verify_files(directory: Path, manifest: Manifest, report: VerifyReport) -> None:
    for entry in manifest.files:
        report.files.append(_check_file(directory, entry.name, entry.sha256, entry.size))
    known = {f.name for f in manifest.files} | ALLOWED_EXTRA
    for path in sorted(directory.iterdir()):
        if path.name not in known and not path.name.startswith("."):
            report.unexpected_files.append(path.name)


async def dump_catalog(dump: Path, *, pg_bin_dir: Path | None = None, min_major: int | None = None) -> DumpCheck:
    """``pg_restore --list`` of a custom-format dump (catalog readable, entries, extensions)."""
    try:
        pg_restore = find_pg_tool("pg_restore", bin_dir=pg_bin_dir, min_major=min_major)
    except BackupError as exc:
        return DumpCheck(ok=False, problem=exc.message)
    result = await runner.run([pg_restore, "--list", "--", str(dump)], env=runner.base_env(), limit_seconds=TOC_TIMEOUT_SECONDS)
    if not result.ok:
        return DumpCheck(ok=False, problem=f"pg_restore --list failed: {result.error_text()}")
    toc = filter_toc(result.stdout)
    entries = [ln for ln in result.stdout.splitlines() if ln.strip() and not ln.lstrip().startswith(";")]
    table_data = sum(1 for ln in entries if " TABLE DATA " in ln)
    return DumpCheck(ok=bool(entries), toc_entries=len(entries), table_data_entries=table_data, extensions=toc.extensions)


async def verify_set_dir(
    directory: Path,
    *,
    signing_key: bytes | None = None,
    deep: bool = True,
    location: str | None = None,
    pg_bin_dir: Path | None = None,
) -> tuple[VerifyReport, Manifest | None]:
    """Verify ``directory``; never raises for a bad set – problems are reported (``ok`` False)."""
    report = VerifyReport(set_id=directory.name, ok=False, deep=deep, location=location or str(directory))
    try:
        manifest, signature = await asyncio.to_thread(read_manifest, directory, signing_key=signing_key)
    except BackupError as exc:
        report.problems.append(exc.message)
        return report, None
    report.set_id = manifest.set_id
    report.signature = signature
    await asyncio.to_thread(_verify_files, directory, manifest, report)
    report.problems += [f"{f.name}: {f.problem}" for f in report.files if not f.ok]
    if deep:
        for name, info in ((CONFIG_TAR, manifest.config), (ARTIFACTS_TAR, manifest.artifacts)):
            if manifest.file(name) is None:
                report.problems.append(f"{name} is not listed in the manifest")
                continue
            if report.failed(name):
                continue  # a modified archive is not inspected further
            tar = await asyncio.to_thread(inspect_tarball, directory / name)
            check = _archive_check(name, tar, info, manifest)
            report.archives.append(check)
            report.problems += [f"{name}: {p}" for p in check.problems]
        if manifest.file(DB_DUMP) is None:
            report.problems.append(f"{DB_DUMP} is not listed in the manifest")
        elif not report.failed(DB_DUMP):
            report.dump = await dump_catalog(directory / DB_DUMP, pg_bin_dir=pg_bin_dir, min_major=manifest.database.server_major)
            if not report.dump.ok:
                report.problems.append(f"{DB_DUMP}: {report.dump.problem or 'empty catalog'}")
            elif manifest.database.toc_entries and report.dump.toc_entries != manifest.database.toc_entries:
                report.problems.append(
                    f"{DB_DUMP}: {report.dump.toc_entries} catalog entries, manifest says {manifest.database.toc_entries}"
                )
    report.ok = not report.problems
    return report, manifest
