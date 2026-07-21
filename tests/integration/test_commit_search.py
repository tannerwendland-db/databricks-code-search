"""Integration tests for ``commit:`` git-hash search (resolution + scoped equivalence).

Requires a running Postgres (standard PG* env). Mirrors ``test_service.py``'s throwaway-schema
+ PGOPTIONS idiom: ``search_code_payload`` opens its OWN connection per call, so the schema must
be visible to every new pooled connection, not just one held-open one.

The fixture deliberately indexes the matching content on a NON-default branch (``release-2.1``,
while the repo's default is ``main``): this is the feature's primary use case and the case that
would silently return zero rows if a ``commit:`` scope failed to suppress the implicit
default-branch conjunct. The repo name is metacharacter-free because ``repo:`` lowers to a ``~*``
regex, so the hand-written equivalence query anchors it as ``repo:^acmewidgets$``.

CI-only (this repo's Postgres is CI's service container); validated locally by lint/type-check +
``--collect-only``, not execution.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any, NamedTuple

import pytest
from sqlalchemy import insert, text
from sqlalchemy.engine import Engine

from app import service
from app.config import Settings
from app.db.client import create_db_engine
from app.db.models import Base, File, Repo, RepoBranch
from indexer.hashing import content_sha

SCHEMA_PREFIX = "test_commit_search"

# Distinct full SHAs (lowercase hex). RELEASE_SHA/HOTFIX_SHA share a 7-char prefix so a prefix
# query collides across two branches (multi-resolution / prefix-collision, AC4).
MAIN_SHA = "aaaaaa1000000000000000000000000000000000"
RELEASE_SHA = "bbbbbb2000000000000000000000000000000000"
HOTFIX_SHA = "bbbbbb2999999999999999999999999999999999"
SHARED_PREFIX = "bbbbbb2"  # matches both RELEASE_SHA and HOTFIX_SHA


class Seeded(NamedTuple):
    engine: Engine
    cfg: Settings


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _cfg() -> Settings:
    return Settings(
        lakebase_endpoint=None,
        statement_timeout_ms=5000,
        max_content_bytes=8 * 1024 * 1024,
        row_limit=200,
        max_row_limit=1000,
        semantic_enabled=False,
    )


def _add_file(conn: Any, repo_id: int, path: str, branches: list[str]) -> None:
    content = f"// foo lives in {path}\n"
    conn.execute(
        insert(File).values(
            repo_id=repo_id,
            path=path,
            lang="go",
            content=content,
            content_sha=content_sha(content + "".join(branches)),
            branches=branches,
        )
    )


@pytest.fixture
def seeded() -> Iterator[Seeded]:
    """One repo (default ``main``) indexed on three branches, foo-matching files on each.

    ``release-2.1`` and ``hotfix`` share a commit-prefix so a prefix query resolves to both.
    """
    schema = _unique(SCHEMA_PREFIX)
    admin_engine = create_db_engine()
    admin_conn = admin_engine.connect()
    prev_pgoptions = os.environ.get("PGOPTIONS")
    engine: Engine | None = None
    try:
        admin_conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        admin_conn.execute(text(f"CREATE SCHEMA {schema}"))
        admin_conn.execute(text(f"SET search_path TO {schema}, public"))
        admin_conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        admin_conn.commit()

        Base.metadata.create_all(bind=admin_conn)
        admin_conn.commit()

        repo_id = admin_conn.execute(
            insert(Repo).values(name="acmewidgets", default_branch="main").returning(Repo.id)
        ).scalar_one()

        _add_file(admin_conn, repo_id, "src/on_main.go", ["main"])
        _add_file(admin_conn, repo_id, "src/on_release.go", ["release-2.1"])
        _add_file(admin_conn, repo_id, "src/on_hotfix.go", ["hotfix"])
        for branch, sha in (
            ("main", MAIN_SHA),
            ("release-2.1", RELEASE_SHA),
            ("hotfix", HOTFIX_SHA),
        ):
            admin_conn.execute(
                insert(RepoBranch).values(repo_id=repo_id, branch=branch, last_indexed_commit=sha)
            )
        admin_conn.commit()

        os.environ["PGOPTIONS"] = f"-c search_path={schema},public"
        engine = create_db_engine()
        yield Seeded(engine=engine, cfg=_cfg())
    finally:
        if engine is not None:
            engine.dispose()
        if prev_pgoptions is None:
            os.environ.pop("PGOPTIONS", None)
        else:
            os.environ["PGOPTIONS"] = prev_pgoptions
        admin_conn.rollback()
        admin_conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        admin_conn.commit()
        admin_conn.close()
        admin_engine.dispose()


def _files(payload: dict[str, Any]) -> list[str]:
    return sorted(f["file"] for f in payload["files"])


# --------------------------------------------------------------------------- reverse lookup


@pytest.mark.integration
def test_bare_commit_resolves_non_default_branch(seeded: Seeded) -> None:
    # AC2: bare commit:<prefix> returns the resolution + empty files, even for a NON-default
    # branch (the default-branch conjunct must not swallow it).
    payload = service.search_code_payload(
        seeded.engine, seeded.cfg, f"commit:{RELEASE_SHA[:9]}", 200
    )
    assert payload["files"] == []
    assert payload["commit_not_indexed"] is False
    (resolved,) = payload["resolved"]
    assert resolved["repo"] == "acmewidgets"
    assert resolved["branch"] == "release-2.1"
    assert resolved["commit"] == RELEASE_SHA


@pytest.mark.integration
def test_bare_commit_no_match_flags_not_indexed(seeded: Seeded) -> None:
    # AC5: a hash matching no indexed branch -> empty resolution + commit_not_indexed, never an
    # unfiltered search.
    payload = service.search_code_payload(seeded.engine, seeded.cfg, "commit:0000000", 200)
    assert payload["files"] == []
    assert payload["resolved"] == []
    assert payload["commit_not_indexed"] is True


# ----------------------------------------------------------------- scoped-search equivalence


@pytest.mark.integration
def test_scoped_commit_equals_repo_branch_query_non_default(seeded: Seeded) -> None:
    # AC3: commit:<prefix> foo == repo:^name$ branch:"release-2.1" foo, on a NON-default branch.
    commit_payload = service.search_code_payload(
        seeded.engine, seeded.cfg, f"commit:{RELEASE_SHA[:9]} foo", 200
    )
    branch_payload = service.search_code_payload(
        seeded.engine, seeded.cfg, 'repo:^acmewidgets$ branch:"release-2.1" foo', 200
    )
    assert _files(commit_payload) == _files(branch_payload) == ["src/on_release.go"]
    # scoping worked: the default-branch file is NOT in the commit-scoped result.
    assert "src/on_main.go" not in _files(commit_payload)
    # per-file commit metadata (AC9) is the resolved head.
    (entry,) = commit_payload["files"]
    assert entry["commit"] == RELEASE_SHA
    assert entry["permalink_branch"] == "release-2.1"


@pytest.mark.integration
def test_prefix_collision_unions_all_resolutions(seeded: Seeded) -> None:
    # AC4: a prefix hitting two branches resolves to BOTH and scopes to the union of pairs;
    # equivalent to the OR-of-(repo,branch) form.
    commit_payload = service.search_code_payload(
        seeded.engine, seeded.cfg, f"commit:{SHARED_PREFIX} foo", 200
    )
    or_payload = service.search_code_payload(
        seeded.engine,
        seeded.cfg,
        '(repo:^acmewidgets$ branch:"release-2.1" OR repo:^acmewidgets$ branch:"hotfix") foo',
        200,
    )
    assert _files(commit_payload) == _files(or_payload) == ["src/on_hotfix.go", "src/on_release.go"]
    resolved_branches = sorted(r["branch"] for r in commit_payload["resolved"])
    assert resolved_branches == ["hotfix", "release-2.1"]
