"""memory entries become Hermclaw's knowledge (D-019)

``memory_entries`` was reserved and never written (Bauplan §10). It now holds lessons, research findings, incidents,
tool-bug suspicions and conventions that planner and coder look up and the operator edits on the "Wissen" page:

* ``enabled`` is replaced by ``status`` (candidate/active/retired); an enabled row – there are none – becomes active.
* title, source, scope, tags, failure signature, occurrence and recall counters, originating job, evidence, the
  embedding model of the vector (vectors of different models are not comparable), expiry and ``updated_at``.
* HNSW cosine index on the embedding, as for ``code_chunks``.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-21 02:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET LOCAL max_parallel_maintenance_workers = 0")  # as in 0001: serial index builds (small /dev/shm)
    op.add_column("memory_entries", sa.Column("title", sa.String(length=300), server_default=sa.text("''"), nullable=False))
    op.add_column("memory_entries", sa.Column("status", sa.String(length=16), server_default=sa.text("'candidate'"), nullable=False))
    op.execute("UPDATE memory_entries SET status = 'active' WHERE enabled")
    op.drop_column("memory_entries", "enabled")
    op.add_column("memory_entries", sa.Column("source", sa.String(length=32), server_default=sa.text("'system'"), nullable=False))
    op.add_column("memory_entries", sa.Column("scope", sa.String(length=300), nullable=True))
    op.add_column(
        "memory_entries",
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    )
    op.add_column("memory_entries", sa.Column("signature", sa.String(length=300), nullable=True))
    op.add_column("memory_entries", sa.Column("occurrences", sa.Integer(), server_default=sa.text("1"), nullable=False))
    op.add_column("memory_entries", sa.Column("recalls", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.add_column("memory_entries", sa.Column("job_id", sa.UUID(), nullable=True))
    op.add_column(
        "memory_entries",
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.add_column("memory_entries", sa.Column("embedding_model", sa.String(length=100), nullable=True))
    op.add_column("memory_entries", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("memory_entries", sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False))
    op.create_foreign_key(op.f("fk_memory_entries_job_id_jobs"), "memory_entries", "jobs", ["job_id"], ["id"], ondelete="SET NULL")
    op.create_index(op.f("ix_memory_entries_job_id"), "memory_entries", ["job_id"], unique=False)
    op.create_index(op.f("ix_memory_entries_signature"), "memory_entries", ["signature"], unique=False)
    op.create_index("ix_memory_entries_kind_status", "memory_entries", ["kind", "status"], unique=False)
    op.create_index(
        "ix_memory_entries_embedding_hnsw",
        "memory_entries",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_memory_entries_embedding_hnsw",
        table_name="memory_entries",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.drop_index("ix_memory_entries_kind_status", table_name="memory_entries")
    op.drop_index(op.f("ix_memory_entries_signature"), table_name="memory_entries")
    op.drop_index(op.f("ix_memory_entries_job_id"), table_name="memory_entries")
    op.drop_constraint(op.f("fk_memory_entries_job_id_jobs"), "memory_entries", type_="foreignkey")
    op.add_column("memory_entries", sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.execute("UPDATE memory_entries SET enabled = (status = 'active')")
    for column in (
        "updated_at",
        "expires_at",
        "embedding_model",
        "evidence",
        "job_id",
        "recalls",
        "occurrences",
        "signature",
        "tags",
        "scope",
        "source",
        "status",
        "title",
    ):
        op.drop_column("memory_entries", column)
