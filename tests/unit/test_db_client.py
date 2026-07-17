"""Unit tests for the database engine factory and ORM models.

These are hermetic: local (PGHOST) mode must build an engine without importing or
touching the Databricks SDK, and the models must expose exactly the durable-core
schema (repos / files / symbols) with the expected FKs, unique constraint, and
pg_trgm GIN indexes.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.engine import Engine

from app.db.client import _local_url, create_db_engine
from app.db.models import Base


@pytest.fixture
def local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGHOST", "localhost")
    monkeypatch.setenv("PGPORT", "5432")
    monkeypatch.setenv("PGUSER", "codesearch")
    monkeypatch.setenv("PGPASSWORD", "s3cret")
    monkeypatch.setenv("PGDATABASE", "codesearch")


@pytest.mark.unit
def test_local_url_driver_host_dbname(local_env: None) -> None:
    url = _local_url()
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "localhost"
    assert url.port == 5432
    assert url.username == "codesearch"
    assert url.database == "codesearch"


@pytest.mark.unit
def test_create_engine_local_mode_never_touches_sdk(
    local_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("Databricks SDK must not be used in local (PGHOST) mode")

    # If the local path so much as instantiates a WorkspaceClient, this fails.
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", _boom)

    engine = create_db_engine()
    try:
        assert isinstance(engine, Engine)
        assert engine.url.drivername == "postgresql+psycopg"
        assert engine.url.database == "codesearch"
    finally:
        engine.dispose()


@pytest.mark.unit
def test_metadata_has_exactly_durable_core_tables() -> None:
    assert set(Base.metadata.tables) == {"repos", "files", "symbols"}


@pytest.mark.unit
def test_repo_name_unique() -> None:
    repos = Base.metadata.tables["repos"]
    assert repos.c.name.unique is True


@pytest.mark.unit
def test_files_foreign_key_to_repos() -> None:
    files = Base.metadata.tables["files"]
    targets = {fk.column.table.name for fk in files.c.repo_id.foreign_keys}
    assert "repos" in targets
    fk = next(iter(files.c.repo_id.foreign_keys))
    assert fk.ondelete == "CASCADE"


@pytest.mark.unit
def test_files_unique_repo_id_path() -> None:
    files = Base.metadata.tables["files"]
    unique_cols = [
        {c.name for c in c.columns} for c in files.constraints if isinstance(c, UniqueConstraint)
    ]
    assert {"repo_id", "path"} in unique_cols


@pytest.mark.unit
def test_symbols_foreign_keys() -> None:
    symbols = Base.metadata.tables["symbols"]
    file_fk = next(iter(symbols.c.file_id.foreign_keys))
    assert file_fk.column.table.name == "files"
    assert file_fk.ondelete == "CASCADE"
    repo_fk = next(iter(symbols.c.repo_id.foreign_keys))
    assert repo_fk.column.table.name == "repos"


@pytest.mark.unit
def test_trgm_gin_indexes_declared() -> None:
    expected = {
        "ix_files_content_trgm": ("files", "content"),
        "ix_files_path_trgm": ("files", "path"),
        "ix_symbols_name_trgm": ("symbols", "name"),
    }
    declared: dict[str, Index] = {}
    for table in Base.metadata.tables.values():
        for idx in table.indexes:
            declared[idx.name or ""] = idx

    for name, (table_name, column) in expected.items():
        assert name in declared, f"missing index {name}"
        idx = declared[name]
        assert idx.table.name == table_name
        assert idx.dialect_options["postgresql"]["using"] == "gin"
        assert idx.dialect_options["postgresql"]["ops"] == {column: "gin_trgm_ops"}
