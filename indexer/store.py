"""Atomic per-(repo, branch) upsert + content-SHA-keyed mark-and-sweep.

The caller supplies a live :class:`sqlalchemy.Connection` (mirroring the injected
connection seam in ``scripts/migrate.py``); ``index_repo`` owns the single atomic
unit of work via ``with conn.begin():`` for ONE branch. A mid-run failure rolls
the whole (repo, branch) transaction back, so the destructive sweep can never run
against a partially-written index (plan Option A). The caller's ``search_path``
is preserved -- this module never opens its own engine.

Multi-branch (0003+): ``files`` is content-deduped on ``(repo_id, path,
content_sha)``, with membership in a GIN-indexed ``branches`` array rather than
one row per (repo, path). The caller is expected to index a repo's branches
SEQUENTIALLY within one worker (plan Option A1: per-repo fan-out, branches
serial) -- that is what makes the sweep's plain ``UPDATE``/``DELETE`` safe
without an advisory lock: no other writer can touch this repo's rows
concurrently. The per-``(repo, branch)`` CAS baseline now lives on
``repo_branches``, not ``repos`` -- the old repos-level ``StaleIndexError`` guard
is retired (see ``app/alembic/versions/0003_multi_branch.py``); only the
default-branch run writes the deprecated ``repos`` legacy stamp, and it does so
WITHOUT a CAS check (one release's grace period for 0002-era readers).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from sqlalchemy import Connection, delete, func, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import INDEX_SEMANTICS_VERSION, File, Repo, RepoBranch, Symbol
from indexer.hashing import content_sha
from indexer.languages import ExtractedSymbol, IndexCounts, ParsedFile

logger = logging.getLogger("indexer.store")


class StaleIndexError(RuntimeError):
    """The ``repo_branches`` row changed between this transaction's first and last statement.

    An invariant assertion, not an expected failure path: under the current
    single-run job model no second writer for one ``(repo, branch)`` can exist
    (branches within a repo are indexed sequentially by the same worker -- plan
    Option A1). It buys a loud failure the day that property is removed
    (``for_each_task`` sharding, per-branch parallel fan-out, or a raised
    ``max_concurrent_runs``). It protects the *stamp* only -- it does not detect
    a refactor that moves the ``repo_branches`` upsert out of statement 2.
    """


# Called as chunk_writer(conn, repo_id, file_id, pf) once per file, inside the
# same conn.begin() as the rest of that file's row (issue #14 Phase 2). Vectors
# must already be computed -- this seam never calls an embedder itself (A4).
ChunkWriter = Callable[[Connection, int, int, ParsedFile], None]


def index_repo(
    conn: Connection,
    *,
    name: str,
    branch: str,
    is_default: bool,
    head_sha: str,
    items: Iterable[tuple[ParsedFile, list[ExtractedSymbol]]],
    chunk_writer: ChunkWriter | None = None,
) -> IndexCounts:
    """Upsert one ``(repo, branch)``'s files/symbols and sweep this branch's stale membership.

    All work runs inside a single ``with conn.begin():`` transaction:

    1. Upsert the ``repos`` row (keyed on ``name``) -> ``repo_id``. Only when
       ``is_default`` does this set ``default_branch`` and the deprecated legacy
       stamp columns (``last_indexed_commit`` / ``index_semantics_version`` /
       ``last_indexed_at``), written unconditionally (no CAS -- see module
       docstring). The ``ON CONFLICT DO UPDATE`` form is used even on a
       non-default run (a no-op ``SET name=name``) so ``RETURNING id`` always
       yields a row: ``DO NOTHING ... RETURNING`` returns nothing on conflict,
       which would break the ``repo_id`` bootstrap.
    2. Upsert/read the ``repo_branches`` row for ``(repo_id, branch)`` under its
       row lock, capturing ``(baseline_commit, baseline_version)`` -- the CAS
       baseline for step 5, mirroring the same ``RETURNING`` trick as step 1.
    3. Per file: an array-union upsert on ``uq_files_repo_path_sha`` -- a file
       whose content already exists under another branch gets THIS branch
       unioned into its ``branches`` array (one row, shared content); a file
       whose content differs from every existing version gets its own row. Then
       delete-and-reinsert its ``symbols`` (no natural key), then call
       ``chunk_writer`` (if given) so chunk writes commit/roll back with the
       rest of that file's row. Each processed file's ``(path, content_sha)`` is
       collected into this branch's seen-set.
    4. Membership sweep, keyed on THIS branch's seen-set (never on ``commit``,
       which is ambiguous under dedup): strip ``branch`` from any row's
       ``branches`` array that is not in the seen-set, then delete any row left
       with an empty array (cascades ``symbols``/``chunks``). Pure DML, no
       ``TEMP TABLE`` (the job role has no guaranteed database-level TEMP
       privilege on Lakebase). **Skipped (with a WARNING) when the parsed file
       set is empty** -- an empty seen-set would otherwise strip ``branch`` from
       every row in the repo; conservatively skipping is safer than wiping.
    5. CAS-stamp the ``repo_branches`` row for ``(repo_id, branch)`` against the
       step-2 baseline (raises :class:`StaleIndexError` on mismatch).

    ``items`` may be a lazy generator; it is consumed inside the open
    transaction so memory stays bounded. ``chunk_writer`` defaults to ``None``,
    which makes this byte-identical to the core (semantic-off) path (issue #14
    AC-1); when given, it must write PRECOMPUTED chunks -- embeddings are
    computed outside this transaction (issue #14 A4), so no network call ever
    happens here.
    """
    file_count = 0
    symbol_count = 0
    seen_paths: list[str] = []
    seen_shas: list[str] = []

    with conn.begin():
        # MUST REMAIN STATEMENT 1 of this transaction -- see the docstring.
        repo_values: dict[str, object] = {"name": name}
        repo_set: dict[str, object] = {"name": name}
        if is_default:
            repo_values.update(
                default_branch=branch,
                last_indexed_commit=head_sha,
                index_semantics_version=INDEX_SEMANTICS_VERSION,
                last_indexed_at=func.now(),
            )
            repo_set = {k: v for k, v in repo_values.items() if k != "name"}
        repo_stmt = (
            pg_insert(Repo)
            .values(**repo_values)
            .on_conflict_do_update(index_elements=[Repo.name], set_=repo_set)
            .returning(Repo.id)
        )
        repo_id = conn.execute(repo_stmt).scalar_one()

        # Statement 2: the per-branch CAS baseline, on repo_branches now, not
        # repos. Same no-op-SET-on-conflict trick as statement 1.
        branch_stmt = (
            pg_insert(RepoBranch)
            .values(repo_id=repo_id, branch=branch)
            .on_conflict_do_update(
                constraint="uq_repo_branches",
                set_={"branch": branch},
            )
            .returning(RepoBranch.last_indexed_commit, RepoBranch.index_semantics_version)
        )
        baseline_commit, baseline_version = conn.execute(branch_stmt).one()

        for pf, syms in items:
            sha = content_sha(pf.content)
            file_stmt = (
                pg_insert(File)
                .values(
                    repo_id=repo_id,
                    path=pf.path,
                    lang=pf.lang,
                    size=pf.size,
                    content=pf.content,
                    commit=head_sha,
                    content_sha=sha,
                    branches=[branch],
                )
                .on_conflict_do_update(
                    constraint="uq_files_repo_path_sha",
                    set_={
                        "lang": pf.lang,
                        "size": pf.size,
                        "content": pf.content,
                        "commit": head_sha,
                        # Union this branch into whatever branches already share
                        # this exact content version -- a plain UNION via
                        # unnest+array_agg, row-lock-atomic regardless of
                        # concurrent readers (there is no concurrent WRITER for
                        # this repo -- see module docstring).
                        "branches": text(
                            "(SELECT array_agg(DISTINCT e) FROM "
                            "unnest(files.branches || excluded.branches) e)"
                        ),
                    },
                )
                .returning(File.id)
            )
            file_id = conn.execute(file_stmt).scalar_one()
            file_count += 1
            seen_paths.append(pf.path)
            seen_shas.append(sha)

            conn.execute(delete(Symbol).where(Symbol.file_id == file_id))
            if syms:
                conn.execute(
                    pg_insert(Symbol),
                    [
                        {
                            "file_id": file_id,
                            "repo_id": repo_id,
                            "name": s.name,
                            "kind": s.kind,
                            "start_line": s.start_line,
                            "end_line": s.end_line,
                        }
                        for s in syms
                    ],
                )
                symbol_count += len(syms)

            if chunk_writer is not None:
                chunk_writer(conn, repo_id, file_id, pf)

        swept = _sweep_membership(
            conn,
            name=name,
            branch=branch,
            repo_id=repo_id,
            seen_paths=seen_paths,
            seen_shas=seen_shas,
        )

        _stamp_repo_branch(
            conn,
            name=name,
            branch=branch,
            repo_id=repo_id,
            head_sha=head_sha,
            baseline_commit=baseline_commit,
            baseline_version=baseline_version,
        )

    return IndexCounts(files=file_count, symbols=symbol_count, swept=swept)


def _sweep_membership(
    conn: Connection,
    *,
    name: str,
    branch: str,
    repo_id: int,
    seen_paths: list[str],
    seen_shas: list[str],
) -> int:
    """Strip ``branch`` from any row not in this run's seen-set, then delete emptied rows.

    Pure DML via ``unnest`` of two parallel bound arrays -- no ``TEMP TABLE``
    (the job role has no guaranteed database-level TEMP privilege on Lakebase,
    see ``app/db/grants.py``). Expressed as raw SQL rather than SQLAlchemy Core:
    the anti-join against a two-column ``unnest(...)`` table-valued function has
    no materially clearer Core-expression form, and the exact shape here is what
    the plan's design and its review-hardened pre-mortem (#5, the empty-seen-set
    guard below) were validated against.

    **Empty seen-set guard**: if this branch parsed zero indexable files,
    skipping this sweep (WARN, return 0) is the conservative choice -- running
    it would strip ``branch`` from every row in the repo, wiping a branch's
    entire membership on a transient empty parse. A genuinely emptied branch
    simply retains stale membership until it next indexes non-empty (the same
    stale-not-wrong tradeoff as the retired-branch-reconciliation gap tracked in
    ``.omc/plans/open-questions.md``).
    """
    if not seen_paths:
        logger.warning(
            "%s@%s: parsed 0 indexable files; skipping the membership sweep "
            "(an empty seen-set would strip this branch from every file in the repo)",
            name,
            branch,
        )
        return 0

    removed = conn.execute(
        text(
            "UPDATE files SET branches = array_remove(branches, :branch) "
            "WHERE repo_id = :repo_id AND :branch = ANY(branches) "
            "AND NOT EXISTS (SELECT 1 FROM unnest(CAST(:paths AS text[]), CAST(:shas AS text[])) "
            "AS t(p, s) WHERE t.p = files.path AND t.s = files.content_sha)"
        ),
        {"branch": branch, "repo_id": repo_id, "paths": seen_paths, "shas": seen_shas},
    ).rowcount
    # The DELETE below only ever catches rows the UPDATE just emptied (no row
    # can already be at cardinality 0 entering a sweep -- every prior sweep
    # cleans those up too), so its rowcount is a SUBSET of ``removed``, not an
    # additional distinct file. ``swept`` counts distinct files this branch's
    # sweep affected -- one file whose only membership was this branch is one
    # swept file, whether it survives with an emptied-then-deleted row or (with
    # another branch still present) merely loses this branch from its array.
    conn.execute(
        text("DELETE FROM files WHERE repo_id = :repo_id AND cardinality(branches) = 0"),
        {"repo_id": repo_id},
    )
    return removed


def _stamp_repo_branch(
    conn: Connection,
    *,
    name: str,
    branch: str,
    repo_id: int,
    head_sha: str,
    baseline_commit: str | None,
    baseline_version: int | None,
) -> None:
    """Compare-and-set the ``repo_branches`` stamp against the statement-2 baseline.

    Raises :class:`StaleIndexError` if the row no longer matches the baseline,
    which propagates out of ``index_repo``'s ``conn.begin()`` and rolls the whole
    ``(repo, branch)`` transaction back rather than regressing the index.
    """
    result = conn.execute(
        update(RepoBranch)
        .where(
            RepoBranch.repo_id == repo_id,
            RepoBranch.branch == branch,
            RepoBranch.last_indexed_commit.is_not_distinct_from(baseline_commit),
            RepoBranch.index_semantics_version.is_not_distinct_from(baseline_version),
        )
        .values(
            last_indexed_commit=head_sha,
            index_semantics_version=INDEX_SEMANTICS_VERSION,
            last_indexed_at=func.now(),
        )
    )
    if result.rowcount != 1:
        raise StaleIndexError(
            f"{name}@{branch}: repo_branches row changed since this transaction's first "
            f"statement (baseline {baseline_commit!r}/{baseline_version!r}); aborting rather "
            "than regressing the index"
        )
