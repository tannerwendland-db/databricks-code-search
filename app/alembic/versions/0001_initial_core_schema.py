"""initial core schema

Revision ID: 0001
Revises:
Create Date: 2026-07-17 19:39:43.541933

Durable core only: repos / files / symbols plus the three pg_trgm GIN indexes.
Phase-4 similarity-search surface is deliberately excluded here. The single
pg_trgm extension is created first so ``gin_trgm_ops`` is resolvable when the GIN
indexes are built.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pg_trgm must exist before the gin_trgm_ops GIN indexes below are created.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "repos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("default_branch", sa.Text(), nullable=True),
        sa.Column("last_indexed_commit", sa.Text(), nullable=True),
        sa.Column("last_indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("lang", sa.Text(), nullable=True),
        sa.Column("size", sa.Integer(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("commit", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["repo_id"], ["repos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repo_id", "path", name="uq_files_repo_id_path"),
    )
    op.create_index(
        "ix_files_content_trgm",
        "files",
        ["content"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"content": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_files_path_trgm",
        "files",
        ["path"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"path": "gin_trgm_ops"},
    )
    op.create_table(
        "symbols",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("start_line", sa.Integer(), nullable=True),
        sa.Column("end_line", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repo_id"], ["repos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_symbols_name_trgm",
        "symbols",
        ["name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )


def downgrade() -> None:
    # Drop the GIN indexes first, then the tables in reverse dependency order
    # (symbols -> files -> repos).
    op.drop_index("ix_symbols_name_trgm", table_name="symbols")
    op.drop_index("ix_files_path_trgm", table_name="files")
    op.drop_index("ix_files_content_trgm", table_name="files")
    op.drop_table("symbols")
    op.drop_table("files")
    op.drop_table("repos")
    # Intentionally NOT dropping the pg_trgm extension: it is a database-wide
    # object that may be shared by other schemas/objects, and re-creating it on
    # upgrade is idempotent.
