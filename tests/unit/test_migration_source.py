"""Static source assertions on the 0001 migration (no database required).

These guard the hand-edited invariants of ``0001_initial_core_schema.py`` that an
accidental re-autogenerate could silently undo: the fixed ``0001`` revision id, the
``pg_trgm`` extension being created before any GIN index, and the deliberate
absence of any Phase-4 vector / tsvector / chunks surface.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_VERSIONS_DIR = Path(__file__).resolve().parents[2] / "app" / "alembic" / "versions"


@pytest.fixture
def source() -> str:
    matches = sorted(_VERSIONS_DIR.glob("0001_*.py"))
    assert len(matches) == 1, f"expected exactly one 0001_*.py, found {matches}"
    return matches[0].read_text()


@pytest.mark.unit
def test_revision_identifiers(source: str) -> None:
    assert 'revision: str = "0001"' in source
    assert "down_revision: str | None = None" in source


@pytest.mark.unit
def test_pg_trgm_created_before_first_index(source: str) -> None:
    ext_pos = source.find("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    idx_pos = source.find("op.create_index(")
    assert ext_pos != -1, "missing CREATE EXTENSION IF NOT EXISTS pg_trgm"
    assert idx_pos != -1, "missing op.create_index("
    assert ext_pos < idx_pos, "pg_trgm must be created before the first op.create_index("


@pytest.mark.unit
def test_no_vector_or_phase4_surface(source: str) -> None:
    for forbidden in ("vector", "tsvector", "chunks", "CREATE EXTENSION vector", "DROP EXTENSION"):
        assert forbidden not in source, f"0001 migration must not reference {forbidden!r}"
