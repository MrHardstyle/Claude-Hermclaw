"""resource request weights, exact verification binding, repo intelligence indexes

* ``resource_requests``: ``weight`` / ``exclusive`` / ``metadata`` of the waiting acquisition (BUG-030: the budget
  queue rule must know whether a higher-priority waiter waits for capacity) + queue index.
* ``verification_runs``: ``workspace_id`` and ``content_digest`` (sha256 over ``<path>\\0<blob sha|deleted>`` of the
  verified files, non-100644 git modes appended to the sha – see ``hermclaw.gitops.engine.content_digest``) so
  ``commit_verified`` can refuse content that changed after verification.
* repository intelligence indexes for large repositories.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-13 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET LOCAL max_parallel_maintenance_workers = 0")  # as in 0001: serial index builds (small /dev/shm)
    # ---- resource_requests (BUG-030)
    op.add_column("resource_requests", sa.Column("weight", sa.Float(), server_default=sa.text("0"), nullable=False))
    op.add_column("resource_requests", sa.Column("exclusive", sa.Boolean(), server_default=sa.text("true"), nullable=False))
    op.add_column(
        "resource_requests",
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.create_index(
        "ix_resource_requests_queue", "resource_requests", ["resource", "state", sa.text("priority DESC"), "created_at"], unique=False
    )
    # ---- verification_runs (exact verification binding)
    op.add_column("verification_runs", sa.Column("workspace_id", sa.UUID(), nullable=True))
    op.add_column("verification_runs", sa.Column("content_digest", sa.String(length=64), nullable=True))
    op.create_index(op.f("ix_verification_runs_workspace_id"), "verification_runs", ["workspace_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_verification_runs_workspace_id_workspaces"),
        "verification_runs",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # ---- repository intelligence (large repositories)
    op.create_index("ix_code_chunks_repo_content_hash", "code_chunks", ["repository_key", "content_hash"], unique=False)
    op.create_index(
        "ix_code_symbols_references_gin",
        "code_symbols",
        ["references"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"references": "jsonb_path_ops"},
    )
    op.create_index("ix_repo_index_runs_repository_key", "repo_index_runs", [sa.text("(stats ->> 'repository_key')")], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_repo_index_runs_repository_key", table_name="repo_index_runs")
    op.drop_index("ix_code_symbols_references_gin", table_name="code_symbols", postgresql_using="gin")
    op.drop_index("ix_code_chunks_repo_content_hash", table_name="code_chunks")
    op.drop_constraint(op.f("fk_verification_runs_workspace_id_workspaces"), "verification_runs", type_="foreignkey")
    op.drop_index(op.f("ix_verification_runs_workspace_id"), table_name="verification_runs")
    op.drop_column("verification_runs", "content_digest")
    op.drop_column("verification_runs", "workspace_id")
    op.drop_index("ix_resource_requests_queue", table_name="resource_requests")
    op.drop_column("resource_requests", "metadata")
    op.drop_column("resource_requests", "exclusive")
    op.drop_column("resource_requests", "weight")
