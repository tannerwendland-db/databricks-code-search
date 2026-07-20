"""Payload builders for the search corpus, shared by the MCP app and the web UI app (issue #35).

Extracted from ``app/main.py`` so a second Databricks App (``webui/``) can call the exact same
search/file/repo-listing logic in-process without importing the MCP server module (which builds
a ``FastMCP``/Starlette ASGI app as an import side effect). ``app/main.py`` keeps ``_``-prefixed
aliases (``_search_code_payload = service.search_code_payload`` etc.) for its own call sites and
existing tests, so this move is a pure relocation: wire shapes, signatures, and behavior are
byte-identical to before.

Pure-ish builders (engine + config in, dict out) so unit tests pin the exact wire shape without
the SDK. Each opens its own connection INSIDE the caller's thread (never shares one across
threads). Shapes are pinned to zoekt parity (``tests/unit/test_main.py``).
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from app.config import Settings
from app.db.models import File, Repo
from app.query.parser import QueryParseError
from app.search.errors import QueryTooBroadError
from app.search.grep import grep_search
from app.search.symbols import SymbolResult, symbol_search


def clamp_limit(limit: int, cfg: Settings) -> int:
    """Clamp a caller-supplied ``limit``: ``<=0`` -> default; ``> max`` -> hard cap."""
    if limit <= 0:
        return cfg.row_limit
    return min(limit, cfg.max_row_limit)


def _repo_name_map(conn: Any) -> dict[int, str]:
    """Map ``repo_id -> Repo.name`` for the current corpus (grep returns ids, not names)."""
    return {row.id: row.name for row in conn.execute(select(Repo.id, Repo.name)).all()}


def _search_envelope(
    query: str,
    *,
    files: list[dict[str, Any]],
    file_count: int,
    match_count: int,
    duration_ns: int,
    truncated: bool,
    truncation_reason: str | None,
    regex_incompatible: bool,
    query_too_broad: bool,
    query_parse_error: str | None,
    no_content_atom: bool,
    zero_width_only_atoms: bool,
) -> dict[str, Any]:
    """Build the pinned ``search_code`` envelope (zoekt fields + additive signal fields).

    ``no_content_atom`` -- the query carried no content atom at all (e.g. ``lang:go``), so
    there was nothing to highlight and zero files is a *shape* outcome, not a true negative.
    ``zero_width_only_atoms`` -- content atoms were present but every one provably matches
    zero-width (e.g. ``/^/``), so every span was dropped. Mutually exclusive by construction.

    Both are grep's per-leg fact AND-ed with "the symbol leg did not answer this query", so
    neither ever fires beside results the caller can see. That suppression is what carries
    grep's own invariants (see :class:`app.search.grep.GrepResult`) up to this layer:

        If ``zero_width_only_atoms`` survives suppression then ``sym_answers`` is False, so
        ``sym_result`` is non-None with ``no_symbol_atom=True``, so ``sym_result.symbols`` is
        empty, so ``files`` comes only from grep -- which is empty by grep's invariant. QED

    Additive and permanent: agents may depend on these keys, so they can never be removed.
    """
    return {
        "query": query,
        "file_count": file_count,
        "match_count": match_count,
        "duration_ns": duration_ns,
        "files": files,
        "truncated": truncated,
        "truncation_reason": truncation_reason,
        "regex_incompatible": regex_incompatible,
        "query_too_broad": query_too_broad,
        "query_parse_error": query_parse_error,
        "no_content_atom": no_content_atom,
        "zero_width_only_atoms": zero_width_only_atoms,
    }


def search_code_payload(engine: Engine, cfg: Settings, query: str, limit: int) -> dict[str, Any]:
    """Run grep + symbol search and shape the merged result to the zoekt parity envelope.

    Content matches (grep) and ``sym:`` definition matches (:func:`symbol_search`) are folded
    into one ``files`` -> ``matches`` list grouped by ``(repo_id, path)``, matching zoekt's
    single-tool model where ``sym:`` results ride the normal envelope. A symbol match carries
    ``symbols: [{name, kind}]`` and ``line`` = the definition's first line (no highlight ``text``
    in V1, so ``grep_search("sym:X")`` -- which returns nothing highlight-driven -- is answered
    here). ``QueryParseError`` -> ``query_parse_error`` + empty files; ``QueryTooBroadError``
    (either leg) -> ``query_too_broad`` + ``truncated``. ``repo_id`` is resolved to ``Repo.name``.
    ``byte_ranges`` are our UTF-8 line-local half-open offsets (documented divergence from
    zoekt's char ``start_col``/``end_col``).

    **Query-shape suppression (issue #31).** grep's ``no_content_atom`` /
    ``zero_width_only_atoms`` are per-leg facts; both are ANDed here with ``not sym_answers``
    so neither fires on a query the symbol leg answered. ``sym:Handler`` is filter-only to grep
    but fully answered here, and ``sym:Handler /^/`` is zero-width-only to grep yet returns
    files -- flagging either would contradict the results sitting beside it and train agents to
    ignore the signal.

    ``sym_answers`` treats an UNKNOWN symbol leg (``sym_result is None``, i.e. the leg timed
    out) as answering, which is a proof rather than a precaution: ``None`` arises only from
    ``QueryTooBroadError``, which means the DB was hit, which means the ``if not patterns``
    short-circuit at ``symbols.py:173`` was False, which means the query HAD a ``sym:`` atom.
    The inverted form (``sym_result is not None and ...``) would emit a false flag on exactly
    that timed-out ``sym:`` query. Corollary: suppression can never swallow the filter-only
    case this issue exists to signal -- a query like ``file:.md`` has no ``sym:`` atom, so it
    short-circuits before any DB hit and can never reach ``sym_result is None``.
    """
    with engine.connect() as conn:
        t0 = time.monotonic()
        try:
            result = grep_search(
                conn,
                query,
                row_limit=limit,
                max_content_bytes=cfg.max_content_bytes,
                statement_timeout_ms=cfg.statement_timeout_ms,
            )
        except QueryParseError as error:
            return _search_envelope(
                query,
                files=[],
                file_count=0,
                match_count=0,
                duration_ns=int((time.monotonic() - t0) * 1e9),
                truncated=False,
                truncation_reason=None,
                regex_incompatible=False,
                query_too_broad=False,
                query_parse_error=str(error),
                no_content_atom=False,
                zero_width_only_atoms=False,
            )
        except QueryTooBroadError:
            # The whole query is over the time budget; the symbol leg would time out too.
            return _search_envelope(
                query,
                files=[],
                file_count=0,
                match_count=0,
                duration_ns=int((time.monotonic() - t0) * 1e9),
                truncated=True,
                truncation_reason=None,
                regex_incompatible=False,
                query_too_broad=True,
                query_parse_error=None,
                no_content_atom=False,
                zero_width_only_atoms=False,
            )

        # Symbol leg: sym: definitions the highlight-driven grep path cannot return. A timeout
        # here flags query_too_broad but still returns whatever grep found (partial, not a lie).
        # Note: a `sym:X foo` query runs the compiler candidate scan twice (once per leg), each
        # under its OWN statement_timeout -- so the DB-time bound is per-leg, not a single budget.
        query_too_broad = False
        try:
            sym_result: SymbolResult | None = symbol_search(
                conn,
                query,
                row_limit=limit,
                statement_timeout_ms=cfg.statement_timeout_ms,
            )
        except QueryTooBroadError:
            sym_result = None
            query_too_broad = True

        duration_ns = int((time.monotonic() - t0) * 1e9)

        # Resolve names for every repo present across BOTH legs, bounded by the same
        # transaction-local statement_timeout the other raw SELECTs use (each leg's own SET LOCAL
        # committed with its transaction, so this lookup would otherwise run uncapped).
        repo_ids = {fm.repo_id for fm in result.files}
        if sym_result is not None:
            repo_ids |= {sm.repo_id for sm in sym_result.symbols}
        name_map: dict[int, str] = {}
        if repo_ids:
            with conn.begin():
                conn.exec_driver_sql(
                    f"SET LOCAL statement_timeout = {int(cfg.statement_timeout_ms)}"
                )
                name_map = _repo_name_map(conn)

    # Merge content + symbol matches into one file list grouped by (repo_id, path).
    merged: dict[tuple[int, str], dict[str, Any]] = {}

    def _entry(repo_id: int, path: str, lang: str | None) -> dict[str, Any]:
        entry = merged.get((repo_id, path))
        if entry is None:
            entry = {"repo_id": repo_id, "path": path, "lang": lang, "matches": []}
            merged[(repo_id, path)] = entry
        return entry

    match_count = 0
    for fm in result.files:
        entry = _entry(fm.repo_id, fm.path, fm.lang)
        for lm in fm.line_matches:
            entry["matches"].append(
                {
                    "line": lm.line_number,
                    "text": lm.line_text,
                    "byte_ranges": [[start, end] for start, end in lm.byte_ranges],
                }
            )
        # match_count counts matched spans (byte_ranges), not lines: the golden zoekt fixture
        # reports 2 for one line carrying two ranges.
        match_count += sum(len(lm.byte_ranges) for lm in fm.line_matches)

    if sym_result is not None:
        for sm in sym_result.symbols:
            entry = _entry(sm.repo_id, sm.path, sm.lang)
            entry["matches"].append(
                {
                    "line": sm.start_line,
                    "text": "",  # V1: line + name + kind, no def-line text (documented follow-up)
                    "byte_ranges": [],
                    "symbols": [{"name": sm.name, "kind": sm.kind}],
                }
            )
            match_count += 1  # each symbol definition is one match (its byte_ranges is empty)

    files: list[dict[str, Any]] = []
    for entry in sorted(merged.values(), key=lambda e: (e["repo_id"], e["path"])):
        # Order matches within a file by line; NULL symbol lines sort last.
        entry["matches"].sort(key=lambda m: (m["line"] is None, m["line"] or 0))
        files.append(
            {
                "repo": name_map.get(entry["repo_id"], str(entry["repo_id"])),
                "file": entry["path"],
                "language": entry["lang"],
                "branches": ["HEAD"],  # durable core has no per-branch content
                "matches": entry["matches"],
            }
        )

    # `is None or` is load-bearing: an unknown (timed-out) symbol leg counts as answering, and
    # is provably always the sym-bearing shape. See the docstring.
    sym_answers = sym_result is None or not sym_result.no_symbol_atom
    no_content_atom = result.no_content_atom and not sym_answers
    zero_width_only_atoms = result.zero_width_only_atoms and not sym_answers

    sym_truncated = sym_result.truncated if sym_result is not None else False
    truncated = result.truncated or sym_truncated or query_too_broad
    truncation_reason = result.truncation_reason or (
        sym_result.truncation_reason if sym_result is not None else None
    )
    return _search_envelope(
        query,
        files=files,
        file_count=len(files),
        match_count=match_count,
        duration_ns=duration_ns,
        truncated=truncated,
        truncation_reason=truncation_reason,
        regex_incompatible=result.regex_incompatible,
        query_too_broad=query_too_broad,
        query_parse_error=None,
        no_content_atom=no_content_atom,
        zero_width_only_atoms=zero_width_only_atoms,
    )


def list_repos_payload(engine: Engine, cfg: Settings) -> dict[str, Any]:
    """List indexed repos with metadata, bounded by a transaction-local statement_timeout."""
    with engine.connect() as conn:
        with conn.begin():
            # SET LOCAL (int-coerced -> injection-safe) is transaction-scoped, so it never
            # leaks a statement_timeout onto the pooled connection (unlike a session-level SET).
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(cfg.statement_timeout_ms)}")
            rows = conn.execute(
                select(
                    Repo.name,
                    Repo.default_branch,
                    Repo.last_indexed_at,
                    Repo.last_indexed_commit,
                ).order_by(Repo.name)
            ).all()
    repos = [
        {
            "name": row.name,
            "branches": [row.default_branch] if row.default_branch else ["HEAD"],
            "index_time": row.last_indexed_at.isoformat() if row.last_indexed_at else None,
            "default_branch": row.default_branch,
            "last_indexed_commit": row.last_indexed_commit,
        }
        for row in rows
    ]
    return {"repos": repos, "count": len(repos)}


def get_file_payload(engine: Engine, cfg: Settings, repo: str, path: str) -> dict[str, Any]:
    """Fetch one file's full content by (repo name, path); a miss is a structured signal."""
    with engine.connect() as conn:
        with conn.begin():
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(cfg.statement_timeout_ms)}")
            content = conn.execute(
                select(File.content)
                .join(Repo, File.repo_id == Repo.id)
                .where(Repo.name == repo, File.path == path)
            ).scalar_one_or_none()
    if content is None:
        return {"repo": repo, "path": path, "branch": "HEAD", "content": None, "found": False}
    return {"repo": repo, "path": path, "branch": "HEAD", "content": content, "found": True}
