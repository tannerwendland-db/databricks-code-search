"""Hard gate: prove Postgres ``pgcrypto`` and Python hashing agree.

The ``0003`` migration's in-DB backfill computes
``encode(digest(coalesce(content,''),'sha256'),'hex')``; the indexer's every
subsequent write goes through ``indexer.hashing.content_sha``. If these ever
disagree, the first post-migration index of an unchanged repo would mint a new
``content_sha`` for every file and silently duplicate the corpus. This test is
the gate that decides whether the in-DB backfill is safe to ship, versus
falling back to a two-phase Python backfill.

Also verifies the stronger claim required here: pgcrypto must be usable by
the migrator, not merely listed in ``pg_available_extensions`` -- either it is
already installed, or ``CREATE EXTENSION IF NOT EXISTS pgcrypto`` succeeds under
the same connection ``scripts/migrate.py`` uses.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db.client import create_db_engine
from indexer.hashing import content_sha

_CASES = [
    "ascii",
    "héllo→λ",
    "",
    None,
    "trailing newline\n",
]


@pytest.mark.integration
def test_pgcrypto_creatable_by_migrator() -> None:
    """The migrator identity can either see pgcrypto installed or create it."""
    engine = create_db_engine()
    try:
        with engine.connect() as conn:
            already_installed = conn.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'pgcrypto'")
            ).scalar()
            if already_installed:
                conn.rollback()
                return
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
            conn.commit()
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize("case", _CASES)
def test_content_sha_matches_pgcrypto_digest(case: str | None) -> None:
    engine = create_db_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
            db_hash = conn.execute(
                text("SELECT encode(digest(coalesce(:c,''),'sha256'),'hex')"),
                {"c": case},
            ).scalar()
            conn.rollback()
    finally:
        engine.dispose()

    assert db_hash == content_sha(case)
