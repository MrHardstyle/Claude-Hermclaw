"""Hermclaw's knowledge store (D-019): only active entries reach a model, semantic search with a word-match fallback,
incidents counted once per job, edits re-embedded."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import delete

from hermclaw.core.config import MemoryPolicy
from hermclaw.memory.store import MemoryStore, import_entries, word_score
from hermclaw.persistence.models import Job, MemoryEntry
from tests.integration.test_repo_intelligence_support import HashingEmbedder

SM = Any


@pytest.fixture
async def store(sessionmaker: SM) -> MemoryStore:
    async with sessionmaker() as s:
        await s.execute(delete(MemoryEntry))
        await s.commit()
    return MemoryStore(sessionmaker, HashingEmbedder(), MemoryPolicy(min_score=0.2))


async def _job(sessionmaker: SM) -> uuid.UUID:
    async with sessionmaker() as s:
        job = Job(title="memory test", prompt="p", status="succeeded")
        s.add(job)
        await s.commit()
        return job.id


async def test_only_active_entries_are_found_and_recalls_are_counted(store: MemoryStore) -> None:
    active = await store.add(
        kind="lesson",
        title="Fix every ruff finding",
        content="ruff F401 unused import: remove each finding the lint names, also in tests",
        status="active",
    )
    await store.add(kind="lesson", title="Unconfirmed ruff idea", content="ruff unused import candidate", status="candidate")
    await store.add(kind="note", title="PowerShell arrays", content="a single object is not a JSON array", status="active")
    hits = await store.search("ruff unused import in tests")
    assert [h.id for h in hits] == [active]
    assert hits[0].score > 0.2 and "[lesson] Fix every ruff finding" in hits[0].render(400)
    row = await store.get(active)
    assert row is not None and row.recalls == 1 and row.embedding_model == "fake-hashing-embedder"
    assert await store.search("ruff unused import", statuses=("candidate",), count=False)


async def test_entries_without_a_vector_are_matched_by_their_words(sessionmaker: SM, store: MemoryStore) -> None:
    store.embedder = HashingEmbedder(fail_with=RuntimeError("embedding model down"))
    entry = await store.add(
        kind="tool_bug",
        title="replace_text and blank lines",
        content="blank lines with indentation spaces did not match",
        status="active",
    )
    row = await store.get(entry)
    assert row is not None and row.embedding is None  # stored all the same
    hits = await store.search("replace_text blank lines")
    assert [h.id for h in hits] == [entry]
    assert word_score("replace_text blank", "nothing here") == 0.0


async def test_scoped_entries_are_found_only_in_their_scope(store: MemoryStore) -> None:
    repo_only = await store.add(
        kind="convention", title="Tests live in tests/", content="pytest tests under tests/", status="active", scope="repo:a"
    )
    assert not await store.search("pytest tests", scopes=["repo:b"])
    assert [h.id for h in await store.search("pytest tests", scopes=["repo:a"])] == [repo_only]


async def test_an_incident_is_counted_once_per_job(sessionmaker: SM, store: MemoryStore) -> None:
    j1, j2 = await _job(sessionmaker), await _job(sessionmaker)
    first, seen = await store.record_incident(
        signature="replace_text:TEXT_NOT_FOUND", title="replace refused", content="x", job_id=j1, evidence={"turn": 3}
    )
    again, seen_again = await store.record_incident(
        signature="replace_text:TEXT_NOT_FOUND", title="replace refused", content="x", job_id=j1, evidence={"turn": 7}
    )
    third, seen_third = await store.record_incident(
        signature="replace_text:TEXT_NOT_FOUND", title="replace refused", content="x", job_id=j2, evidence={"turn": 2}
    )
    assert first == again == third and (seen, seen_again, seen_third) == (1, 1, 2)
    row = await store.get(first)
    assert row is not None and row.status == "candidate" and row.evidence["jobs"] == [str(j1), str(j2)]


async def test_an_edit_is_embedded_again_and_checked(store: MemoryStore) -> None:
    entry = await store.add(kind="note", title="Old", content="about sqlite", status="active")
    assert not await store.search("powershell json array", min_score=0.3)
    row = await store.update(entry, title="PowerShell", content="powershell json array of one object", status="active")
    assert row is not None and row.title == "PowerShell"
    assert [h.id for h in await store.search("powershell json array", min_score=0.3)] == [entry]
    with pytest.raises(ValueError, match="status"):
        await store.update(entry, status="maybe")
    with pytest.raises(ValueError, match="unknown"):
        await store.update(entry, embedding=[0.0])
    assert await store.update(uuid.uuid4(), title="x") is None
    assert [e.id for e in await store.entries(q="powershell")] == [entry]


async def test_operator_knowledge_is_imported_once(sessionmaker: SM, store: MemoryStore) -> None:
    items = [
        {"kind": "convention", "title": "Pester on Linux", "content": "define a stub before Mock", "tags": ["pester"]},
        {"title": "Start the entry point", "content": "run python -m pkg once", "scope": "repo:x"},
    ]
    first = await import_entries(sessionmaker, HashingEmbedder(), store.policy, items)
    again = await import_entries(sessionmaker, HashingEmbedder(), store.policy, items)
    assert first == {"added": ["Pester on Linux", "Start the entry point"], "skipped": [], "tagged": []}
    assert again == {"added": [], "skipped": ["Pester on Linux", "Start the entry point"], "tagged": []}
    rows = {e.title: e for e in await store.entries()}
    assert rows["Pester on Linux"].source == "operator" and rows["Pester on Linux"].status == "active"
    assert rows["Start the entry point"].kind == "lesson" and rows["Start the entry point"].scope == "repo:x"
    with pytest.raises(ValueError, match="title and content"):
        await import_entries(sessionmaker, None, store.policy, [{"title": "no text"}])


async def test_an_imported_entry_gains_code_patterns_later_and_bad_patterns_are_refused(sessionmaker: SM, store: MemoryStore) -> None:
    """D-027: a convention imported before code patterns existed gets them from the edited file (tags are only added,
    the text the operator may have edited stays); a pattern that is no regular expression is refused."""
    entry = {"kind": "convention", "title": "SQL LIKE with user text", "content": "escape % and _", "tags": ["sql"]}
    await import_entries(sessionmaker, HashingEmbedder(), store.policy, [entry])
    tagged = {**entry, "content": "changed in the file", "tags": ["sql", r"code:\bLIKE\b"]}
    result = await import_entries(sessionmaker, HashingEmbedder(), store.policy, [tagged])
    assert result == {"added": [], "skipped": [], "tagged": ["SQL LIKE with user text"]}
    [(hit, patterns)] = await store.code_entries()
    assert hit.content == "escape % and _" and patterns[0].search("WHERE title LIKE ?") and not patterns[0].search("like this")
    with pytest.raises(ValueError, match="invalid code pattern"):
        await import_entries(sessionmaker, None, store.policy, [{"title": "bad", "content": "x", "tags": ["code:(unclosed"]}])
