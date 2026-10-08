"""Pure naming helpers: job branch names, ref validation, protected-branch matching, safe directory names."""

from __future__ import annotations

import fnmatch
import hashlib
import re
import unicodedata
import uuid
from collections.abc import Iterable

from hermclaw.core.errors import ValidationFailed

MAX_SLUG_LEN = 40
MAX_BRANCH_LEN = 250
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_SAFE_DIR = re.compile(r"[^A-Za-z0-9._-]+")
_FORBIDDEN_REF_CHARS = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


def slugify(text: str, *, max_len: int = MAX_SLUG_LEN, fallback: str = "job") -> str:
    """ASCII, lowercase, dash separated slug (``"Fix: Login Bug!"`` -> ``"fix-login-bug"``)."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_STRIP.sub("-", ascii_text).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or fallback


def job_short_id(job_id: uuid.UUID) -> str:
    return job_id.hex[:8]


def job_branch_name(prefix: str, job_id: uuid.UUID, title_or_slug: str) -> str:
    """``<prefix><job-short-id>-<slug>`` – deterministic per job so retries reuse the same branch."""
    name = f"{prefix}{job_short_id(job_id)}-{slugify(title_or_slug)}"
    validate_branch_name(name)
    return name


def strip_heads(ref: str) -> str:
    return ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref


def validate_branch_name(name: str) -> str:
    """Python port of ``git check-ref-format --branch`` rules (plus a length limit, no option injection)."""
    problems: list[str] = []
    if not name or len(name) > MAX_BRANCH_LEN:
        problems.append("empty or too long")
    if name.startswith("-"):
        problems.append("must not start with '-'")
    if name.startswith("/") or name.endswith("/") or "//" in name:
        problems.append("invalid slashes")
    if name.endswith(".") or ".." in name or "@{" in name or name == "@":
        problems.append("invalid dot/at sequence")
    if _FORBIDDEN_REF_CHARS.search(name):
        problems.append("contains forbidden characters")
    for part in name.split("/"):
        if part.startswith(".") or part.endswith(".lock"):
            problems.append(f"invalid component '{part}'")
    if name.startswith("refs/") or name == "HEAD":
        problems.append("must be a short branch name")
    if problems:
        raise ValidationFailed(f"invalid branch name {name!r}: {', '.join(problems)}", details={"branch": name, "problems": problems})
    return name


def matches_protected(branch: str, patterns: Iterable[str]) -> str | None:
    """Return the first protected-branch glob (fnmatch, case-sensitive) that matches ``branch``."""
    short = strip_heads(branch)
    for pattern in patterns:
        pat = strip_heads(pattern.strip())
        if pat and fnmatch.fnmatchcase(short, pat):
            return pattern
    return None


def safe_dir_name(name: str, *, max_len: int = 100) -> str:
    """Filesystem-safe, collision-free directory name derived from a repository name."""
    cleaned = _SAFE_DIR.sub("-", name.replace("/", "__")).strip(".-") or "repo"
    if cleaned != name or len(cleaned) > max_len:
        digest = hashlib.sha1(name.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
        cleaned = f"{cleaned[: max_len - 9]}-{digest}"
    return cleaned
