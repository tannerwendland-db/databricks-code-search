"""Integration tests for the hybrid RRF query against real local Postgres (issue #14).

Runs the ``standin`` backend (pgvector ``<=>`` + ``ts_rank_cd``) that CI's
``pgvector/pgvector:pg16`` image supports -- the ``lakebase_ann`` / ``lakebase_bm25``
operators are lakebase-only and are proven by the live smoke leg, not here. This test proves
the fusion PLUMBING (both legs' ranks fuse via ``FULL OUTER JOIN`` + ``1/(k+rank)``), the real
ANN cosine operator, and HNSW index usage (EXPLAIN). The BM25 leg here is ``ts_rank_cd``, an
APPROXIMATION of the production ``lakebase_text`` scorer (documented, plan DR-4).

The stand-in ``chunks`` table (pgvector ``vector`` + generated ``tsvector`` + hnsw/gin indexes)
mirrors the TEST-ONLY revision under ``fixtures/versions_semantic_standin/``; it is built with
raw DDL on the fixture connection so this test needs no beta extension.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, insert, select, text

from app.config import SEMANTIC_EMBEDDING_DIM
from app.db.client import create_db_engine
from app.db.models import Base, File, Repo
from app.search.semantic import build_hybrid_rrf_sql, format_vector_literal
from indexer.chunk_store import write_chunks
from indexer.hashing import content_sha

SCHEMA = "test_semantic_rrf"


def _vec(pairs: dict[int, float]) -> list[float]:
    """A ``SEMANTIC_EMBEDDING_DIM``-wide vector with ``pairs`` (index -> value) set, else 0."""
    v = [0.0] * SEMANTIC_EMBEDDING_DIM
    for i, x in pairs.items():
        v[i] = x
    return v


@pytest.fixture
def seeded() -> Iterator[Connection]:
    """Throwaway schema: core DDL + a pgvector stand-in ``chunks`` seeded with 3 known chunks.

    Chunks are crafted so the ANN and BM25 legs disagree, exercising the ``FULL OUTER JOIN``:
      * A -- embedding aligned with the query vector AND text-relevant (ranks high in BOTH legs)
      * B -- embedding second-closest but NO query term (ANN-only)
      * C -- embedding orthogonal but the most query-term hits (BM25-only)
    """
    engine = create_db_engine()
    conn = engine.connect()
    try:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        conn.execute(text(f"SET search_path TO {SCHEMA}, public"))
        conn.commit()

        # A5 precondition: the CI image must provide HNSW (pgvector >= 0.5.0).
        hnsw = conn.execute(text("SELECT 1 FROM pg_am WHERE amname = 'hnsw'")).first()
        assert hnsw is not None, "pgvector HNSW access method is unavailable (image regressed?)"

        Base.metadata.create_all(bind=conn)
        conn.execute(
            text(
                "CREATE TABLE chunks ("
                "id bigserial PRIMARY KEY, "
                "file_id integer NOT NULL REFERENCES files(id) ON DELETE CASCADE, "
                "chunk_index integer NOT NULL, "
                "content text NOT NULL, "
                f"embedding vector({SEMANTIC_EMBEDDING_DIM}), "
                "ts tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED, "
                "CONSTRAINT uq_chunks_file_id_chunk_index UNIQUE (file_id, chunk_index))"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_chunks_embedding_hnsw ON chunks "
                "USING hnsw (embedding vector_cosine_ops)"
            )
        )
        conn.execute(text("CREATE INDEX ix_chunks_ts_gin ON chunks USING gin (ts)"))
        conn.commit()

        repo_id = conn.execute(
            insert(Repo).values(name="acme/widgets", default_branch="main").returning(Repo.id)
        ).scalar_one()
        file_id = conn.execute(
            insert(File)
            .values(
                repo_id=repo_id,
                path="src/auth.py",
                lang="python",
                content="stub",
                content_sha=content_sha("stub"),
                branches=["main"],
            )
            .returning(File.id)
        ).scalar_one()

        write_chunks(
            conn,
            file_id=file_id,
            chunks=[
                # A ranks high in BOTH legs; B is ANN-only; C is BM25-only.
                (0, "user authentication and login", _vec({0: 1.0})),
                (1, "database connection pooling and retries", _vec({0: 0.7, 1: 0.7})),
                (2, "authentication authentication token authentication", _vec({1: 1.0})),
            ],
        )
        conn.commit()

        yield conn
    finally:
        conn.rollback()
        conn.execute(text("DROP EXTENSION IF EXISTS vector CASCADE"))
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        conn.commit()
        conn.close()
        engine.dispose()


def _params(topk: int, limit: int) -> dict[str, object]:
    return {
        "qvec": format_vector_literal(_vec({0: 1.0})),  # aligned with chunk A
        "qtext": "authentication",
        "topk": topk,
        "k": 60,
        "lim": limit,
    }


@pytest.mark.integration
def test_rrf_fuses_ann_and_bm25_legs(seeded: Connection) -> None:
    # topk=2 so each leg keeps only its top 2 candidates: ANN keeps {A, B}, BM25 keeps {C, A}.
    # The FULL OUTER JOIN must surface all three -- A (both legs), B (ANN-only), C (BM25-only).
    rows = seeded.execute(build_hybrid_rrf_sql("standin"), _params(topk=2, limit=10)).all()

    contents = [r.content for r in rows]
    assert len(rows) == 3, f"expected all three chunks fused, got {contents}"

    # A is in both legs -> highest RRF; the BM25-only (C) and ANN-only (B) both still surface.
    assert rows[0].content == "user authentication and login"
    assert "authentication authentication token authentication" in contents  # BM25-only
    assert "database connection pooling and retries" in contents  # ANN-only

    # Scores are strictly descending and the envelope columns are present.
    scores = [r.rrf_score for r in rows]
    assert scores == sorted(scores, reverse=True)
    assert rows[0].repo == "acme/widgets"
    assert rows[0].path == "src/auth.py"
    assert rows[0].chunk_index == 0


@pytest.mark.integration
def test_ann_leg_uses_hnsw_index_not_seqscan_sort(seeded: Connection) -> None:
    # EXPLAIN the ANN leg's shape: the inner ORDER BY <=> LIMIT must ride the HNSW index, not a
    # Seq Scan + Sort. enable_seqscan=off (transaction-local) makes the assertion meaningful on a
    # tiny table without changing the query the builder emits.
    with seeded.begin():
        seeded.exec_driver_sql("SET LOCAL enable_seqscan = off")
        plan_rows = seeded.execute(
            text("EXPLAIN " + str(build_hybrid_rrf_sql("standin"))), _params(topk=2, limit=10)
        ).all()
    plan = "\n".join(str(r[0]) for r in plan_rows)

    assert "ix_chunks_embedding_hnsw" in plan, f"ANN leg did not use the HNSW index:\n{plan}"
    assert "Index Scan using ix_chunks_embedding_hnsw" in plan, plan


# ------------------------------------------------------------------- branch scoping (0003)


@pytest.mark.integration
def test_rrf_default_query_excludes_feature_only_chunk(seeded: Connection) -> None:
    # A second file, present ONLY on "feature" (not the repo's default "main"), with a chunk
    # aligned to its own distinctive query vector/term. The default (branch=None) query must
    # never surface it -- D1's inner-subquery join+filter, proven end to end.
    repo_id = seeded.execute(select(Repo.id).where(Repo.name == "acme/widgets")).scalar_one()
    feature_file_id = seeded.execute(
        insert(File)
        .values(
            repo_id=repo_id,
            path="src/feature.py",
            lang="python",
            content="stub",
            content_sha=content_sha("feature-stub"),
            branches=["feature"],
        )
        .returning(File.id)
    ).scalar_one()
    write_chunks(
        seeded,
        file_id=feature_file_id,
        chunks=[(0, "gizmo widget only on the feature branch", _vec({2: 1.0}))],
    )
    seeded.commit()

    params = _params(topk=5, limit=10)
    params["qvec"] = format_vector_literal(_vec({2: 1.0}))
    params["qtext"] = "gizmo"

    default_rows = seeded.execute(build_hybrid_rrf_sql("standin"), params).all()
    assert all(r.content != "gizmo widget only on the feature branch" for r in default_rows)


def _seed_null_default_head_chunk(conn: Connection) -> None:
    """A repo whose ``default_branch`` IS NULL, with one chunk tagged ``branches=['HEAD']``.

    Shared by the two NULL-default tests below. Mirrors the compiler's
    ``test_null_default_branch_resolves_to_head`` and the MCP ``get_file`` gamma/nullbranch
    case (plan test-plan item (b): the ``coalesce(...,'HEAD')`` reachability proof must hold
    at all three default-branch sites -- compiler, semantic, ``get_file``).
    """
    null_repo_id = conn.execute(
        insert(Repo).values(name="gamma/nullbranch", default_branch=None).returning(Repo.id)
    ).scalar_one()
    head_file_id = conn.execute(
        insert(File)
        .values(
            repo_id=null_repo_id,
            path="src/head.py",
            lang="python",
            content="stub",
            content_sha=content_sha("head-stub"),
            branches=["HEAD"],
        )
        .returning(File.id)
    ).scalar_one()
    write_chunks(
        conn,
        file_id=head_file_id,
        chunks=[(0, "widget gizmo only reachable on head", _vec({3: 1.0}))],
    )
    conn.commit()


def _null_default_params() -> dict[str, object]:
    params = _params(topk=5, limit=10)
    params["qvec"] = format_vector_literal(_vec({3: 1.0}))
    params["qtext"] = "widget"
    return params


@pytest.mark.integration
def test_rrf_default_query_resolves_null_default_branch_to_head(seeded: Connection) -> None:
    # coalesce(r.default_branch, 'HEAD') = ANY(f.branches): a NULL default_branch must still
    # resolve to 'HEAD' and match a chunk tagged branches=['HEAD'], with no branch= given.
    _seed_null_default_head_chunk(seeded)

    default_rows = seeded.execute(build_hybrid_rrf_sql("standin"), _null_default_params()).all()
    contents = [r.content for r in default_rows]
    assert "widget gizmo only reachable on head" in contents


@pytest.mark.integration
def test_rrf_explicit_branch_head_reaches_null_default_repo_chunk(seeded: Connection) -> None:
    # Symmetry: an explicit branch="HEAD" (the GIN-served f.branches @> ARRAY[:branch] path)
    # must reach the same chunk as the default coalesce path above.
    _seed_null_default_head_chunk(seeded)

    params = _null_default_params()
    params["branch"] = "HEAD"
    branch_rows = seeded.execute(build_hybrid_rrf_sql("standin", branch="HEAD"), params).all()
    contents = [r.content for r in branch_rows]
    assert "widget gizmo only reachable on head" in contents


@pytest.mark.integration
def test_rrf_branch_filter_reaches_feature_only_chunk(seeded: Connection) -> None:
    repo_id = seeded.execute(select(Repo.id).where(Repo.name == "acme/widgets")).scalar_one()
    feature_file_id = seeded.execute(
        insert(File)
        .values(
            repo_id=repo_id,
            path="src/feature.py",
            lang="python",
            content="stub",
            content_sha=content_sha("feature-stub"),
            branches=["feature"],
        )
        .returning(File.id)
    ).scalar_one()
    write_chunks(
        seeded,
        file_id=feature_file_id,
        chunks=[(0, "gizmo widget only on the feature branch", _vec({2: 1.0}))],
    )
    seeded.commit()

    params = _params(topk=5, limit=10)
    params["qvec"] = format_vector_literal(_vec({2: 1.0}))
    params["qtext"] = "gizmo"
    params["branch"] = "feature"

    branch_rows = seeded.execute(build_hybrid_rrf_sql("standin", branch="feature"), params).all()
    contents = [r.content for r in branch_rows]
    assert "gizmo widget only on the feature branch" in contents
    # The pre-existing "main"-only chunks (A/B/C) never carry "feature" membership, so an
    # exact branch: match excludes them even though they exist in the corpus.
    assert "user authentication and login" not in contents
