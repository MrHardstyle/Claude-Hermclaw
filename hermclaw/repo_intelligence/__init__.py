"""Repository intelligence (Bauplan §14, P11): inventory, lexical/structural/semantic retrieval, fusion ranking,
targeted reads and incremental indexing by Git SHA.

Entry point: :class:`hermclaw.repo_intelligence.service.RepoIntelligence` (implements
:class:`hermclaw.core.interfaces.RepoContextProvider`).
"""

from hermclaw.repo_intelligence.config import INDEX_VERSION, RepoIntelConfig
from hermclaw.repo_intelligence.indexer import IndexTarget, RepoIndexer
from hermclaw.repo_intelligence.inventory import build_inventory
from hermclaw.repo_intelligence.lexical import LexicalSearcher
from hermclaw.repo_intelligence.reader import FileReader, ReadResult
from hermclaw.repo_intelligence.schemas import ChunkHit, FileHit, IndexStats, RepoInventory, SymbolRecord
from hermclaw.repo_intelligence.service import BoundRepo, RepoIntelligence, SearchOutcome

__all__ = [
    "INDEX_VERSION",
    "BoundRepo",
    "ChunkHit",
    "FileHit",
    "FileReader",
    "IndexStats",
    "IndexTarget",
    "LexicalSearcher",
    "ReadResult",
    "RepoIndexer",
    "RepoIntelConfig",
    "RepoIntelligence",
    "RepoInventory",
    "SearchOutcome",
    "SymbolRecord",
    "build_inventory",
]
