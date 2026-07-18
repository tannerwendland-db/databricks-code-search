"""Integration tests for the 0001 migration against real local Postgres.

Each test runs the migration in its own throwaway schema via an *injected*
connection (the same path ``scripts/migrate.py`` uses for Lakebase), with the
Alembic version table pinned to that schema. Requires a running Postgres with the
standard PG* env set.

The enforcement test exercises the least-privilege grants on the *same* superuser
connection via ``SET ROLE`` to a NOLOGIN role; opening a second engine would
connect as the CI superuser and silently bypass the grants under test.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import NamedTuple

import psycopg
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.exc import ProgrammingError

from app.db.client import create_db_engine
from app.db.grants import build_app_grants, build_job_grants
from app.db.models import Base


class Migrated(NamedTuple):
    conn: Connection
    schema: str
    config: Config


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def migrated() -> Iterator[Migrated]:
    """Create a throwaway schema, run ``upgrade head`` into it, yield the handle."""
    schema = _unique("test_migr")
    engine = create_db_engine()
    conn = engine.connect()
    try:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(text(f"SET search_path TO {schema}, public"))
        conn.commit()

        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        config.attributes["version_table_schema"] = schema

        command.upgrade(config, "head")
        conn.commit()

        yield Migrated(conn=conn, schema=schema, config=config)
    finally:
        conn.rollback()
        conn.execute(text("RESET ROLE"))
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.commit()
        conn.close()
        engine.dispose()


@pytest.mark.integration
def test_schema_fidelity(migrated: Migrated) -> None:
    conn, schema = migrated.conn, migrated.schema

    tables = (
        conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"),
            {"s": schema},
        )
        .scalars()
        .all()
    )
    assert {"repos", "files", "symbols"} <= set(tables)

    rows = conn.execute(
        text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = :s"),
        {"s": schema},
    ).all()
    by_name = {name: definition for name, definition in rows}
    for idx in ("ix_files_content_trgm", "ix_files_path_trgm", "ix_symbols_name_trgm"):
        assert idx in by_name, f"missing index {idx}"
        assert "USING gin" in by_name[idx]
        assert "gin_trgm_ops" in by_name[idx]

    fk_count = conn.execute(
        text(
            "SELECT count(*) FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON tc.constraint_name = kcu.constraint_name "
            "  AND tc.table_schema = kcu.table_schema "
            "WHERE tc.constraint_type = 'FOREIGN KEY' "
            "  AND tc.table_schema = :s AND tc.table_name = 'files' "
            "  AND kcu.column_name = 'repo_id'"
        ),
        {"s": schema},
    ).scalar()
    assert fk_count and fk_count >= 1

    unique_cols = (
        conn.execute(
            text(
                "SELECT kcu.column_name FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                "  AND tc.table_schema = kcu.table_schema "
                "WHERE tc.constraint_type = 'UNIQUE' "
                "  AND tc.table_schema = :s AND tc.table_name = 'files' "
                "  AND tc.constraint_name = 'uq_files_repo_id_path'"
            ),
            {"s": schema},
        )
        .scalars()
        .all()
    )
    assert {"repo_id", "path"} <= set(unique_cols)


@pytest.mark.integration
@pytest.mark.xfail(
    strict=False,
    reason="Alembic 1.18.5 does not compare GIN opclasses, so the trgm indexes "
    "re-appear as spurious diffs; documents the limitation, not a real drift.",
)
def test_autogenerate_reports_no_diff(migrated: Migrated) -> None:
    conn = migrated.conn
    context = MigrationContext.configure(
        connection=conn,
        opts={"target_metadata": Base.metadata},
    )
    diffs = compare_metadata(context, Base.metadata)
    assert diffs == [], f"unexpected autogenerate diffs: {diffs}"


@pytest.mark.integration
def test_grant_enforcement_via_set_role(migrated: Migrated) -> None:
    conn, schema = migrated.conn, migrated.schema
    app_ro = _unique("app_ro")
    job_rw = _unique("job_rw")
    try:
        conn.execute(text(f"CREATE ROLE {app_ro} NOLOGIN"))
        conn.execute(text(f"CREATE ROLE {job_rw} NOLOGIN"))
        for stmt in build_app_grants(schema, app_ro):
            conn.execute(text(stmt))
        for stmt in build_job_grants(schema, job_rw):
            conn.execute(text(stmt))
        conn.commit()

        # Read-only role: SELECT works, INSERT is denied.
        conn.execute(text(f"SET ROLE {app_ro}"))
        conn.execute(text(f"SET search_path TO {schema}, public"))
        conn.execute(text("SELECT * FROM files")).all()
        # SQLAlchemy wraps the psycopg error; assert the underlying cause.
        with pytest.raises(ProgrammingError) as excinfo:
            conn.execute(text("INSERT INTO repos (name) VALUES ('denied')"))
        assert isinstance(excinfo.value.orig, psycopg.errors.InsufficientPrivilege)
        # The failed statement aborts the tx; rollback also undoes the SET ROLE.
        conn.rollback()

        # Writer role: INSERT works.
        conn.execute(text(f"SET ROLE {job_rw}"))
        conn.execute(text(f"SET search_path TO {schema}, public"))
        conn.execute(text("INSERT INTO repos (name) VALUES ('allowed')"))
        conn.execute(text("RESET ROLE"))
        conn.rollback()
    finally:
        conn.rollback()
        conn.execute(text("RESET ROLE"))
        for role in (app_ro, job_rw):
            conn.execute(text(f"DROP OWNED BY {role} CASCADE"))
            conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        conn.commit()


@pytest.mark.integration
def test_downgrade_drops_tables_but_keeps_extension(migrated: Migrated) -> None:
    conn, schema = migrated.conn, migrated.schema

    command.downgrade(migrated.config, "base")
    conn.commit()

    remaining = (
        conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :s AND table_name IN ('repos', 'files', 'symbols')"
            ),
            {"s": schema},
        )
        .scalars()
        .all()
    )
    assert remaining == []

    trgm = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")).scalar()
    assert trgm == 1, "downgrade must not drop the shared pg_trgm extension"


@pytest.mark.integration
def test_no_vector_extension_installed(migrated: Migrated) -> None:
    count = migrated.conn.execute(
        text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
    ).scalar()
    assert count == 0
