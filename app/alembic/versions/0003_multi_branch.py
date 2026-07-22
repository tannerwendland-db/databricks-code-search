"""multi-branch support

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-20 00:00:00.000000

Content-deduped multi-branch storage: ``files`` gains ``content_sha`` (the
dedup key alongside ``repo_id``/``path``) and ``branches`` (a GIN-indexed
membership array), and a new ``repo_branches`` table becomes the authoritative
per-(repo, branch) CAS registry, replacing the single ``repos``-level stamp.

Backfill strategy: an in-DB ``pgcrypto`` digest. ``pgcrypto``'s
``digest(...,'sha256')`` was proven byte-identical to
``indexer.hashing.content_sha`` (the canonical Python helper every subsequent
indexer write goes through) across ascii, multibyte UTF-8, empty, ``NULL``, and
trailing-newline content
(``tests/integration/test_content_sha_parity.py``). A single existing repo's
files backfill to one content version tagged with that repo's own default
branch (``coalesce(repos.default_branch,'HEAD')``) -- this ``coalesce`` is
byte-identical to the one used at the other three default-branch sites (query
compiler, semantic default leg, ``get_file``).

No ``INDEX_SEMANTICS_VERSION`` bump: multi-branch does not change symbol/chunk
*meaning*, and the backfill yields correct single-branch state, so forcing a
reindex of every repo is unnecessary.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("files", sa.Column("content_sha", sa.Text(), nullable=True))
    op.add_column("files", sa.Column("branches", postgresql.ARRAY(sa.Text()), nullable=True))

    # pgcrypto must exist before digest() below (proven usable by the migrator identity).
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(
        "UPDATE files SET content_sha = encode(digest(coalesce(content,''),'sha256'),'hex') "
        "WHERE content_sha IS NULL"
    )
    op.execute(
        "UPDATE files f SET branches = ARRAY[coalesce(r.default_branch,'HEAD')] "
        "FROM repos r WHERE r.id = f.repo_id AND f.branches IS NULL"
    )

    op.alter_column("files", "content_sha", nullable=False)
    op.alter_column("files", "branches", nullable=False)

    op.drop_constraint("uq_files_repo_id_path", "files", type_="unique")
    op.create_unique_constraint(
        "uq_files_repo_path_sha", "files", ["repo_id", "path", "content_sha"]
    )
    op.create_index(
        "ix_files_branches_gin", "files", ["branches"], unique=False, postgresql_using="gin"
    )

    op.create_table(
        "repo_branches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("last_indexed_commit", sa.Text(), nullable=True),
        sa.Column("index_semantics_version", sa.Integer(), nullable=True),
        sa.Column("last_indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["repo_id"], ["repos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repo_id", "branch", name="uq_repo_branches"),
    )
    # Seed one default-branch registry row per existing repo, carrying its legacy
    # stamp forward so a repo that has already indexed is not seen as unindexed.
    op.execute(
        "INSERT INTO repo_branches (repo_id, branch, last_indexed_commit, "
        "index_semantics_version, last_indexed_at) "
        "SELECT id, coalesce(default_branch,'HEAD'), last_indexed_commit, "
        "index_semantics_version, last_indexed_at FROM repos"
    )


def downgrade() -> None:
    # Guard FIRST: uq_files_repo_id_path cannot be restored if any path now has
    # more than one content version -- that is exactly what multi-branch dedup
    # produces, and silently collapsing rows to satisfy the old constraint would
    # destroy branch-divergent content. Fail loudly before touching anything.
    dup = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM files GROUP BY repo_id, path HAVING count(*) > 1 LIMIT 1"))
        .first()
    )
    if dup is not None:
        raise RuntimeError(
            "cannot downgrade 0003: multi-branch data present (a path has multiple "
            "content versions); collapse to single-branch before downgrading"
        )

    op.drop_table("repo_branches")
    op.drop_index("ix_files_branches_gin", table_name="files")
    op.drop_constraint("uq_files_repo_path_sha", "files", type_="unique")
    op.create_unique_constraint("uq_files_repo_id_path", "files", ["repo_id", "path"])
    op.drop_column("files", "branches")
    op.drop_column("files", "content_sha")
