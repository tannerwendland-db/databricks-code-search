"""Parser AST -> SQLAlchemy Core ``select`` over ``files``.

Lowers the immutable AST produced by :mod:`app.query.parser` into a single, pure
:class:`sqlalchemy.Select`. The compiler needs no DB connection, so unit tests render
SQL via ``stmt.compile(dialect=postgresql.dialect())``. Each node type lowers to a
``ColumnElement[bool]`` so ``And``/``Or`` compose with ``and_()``/``or_()``.

Trigram acceleration is the point: content/path/symbol-name predicates lower to
operators the GIN ``gin_trgm_ops`` indexes can serve (``ILIKE``/``LIKE``/``~*``/``~``),
never wrapping the indexed column in a function.

Contract / divergence notes (load-bearing):

* Case propagation. The frozen parser stamps ``case_sensitive`` only on
  ``Substring``/``Regex`` -- filters carry no case flag. Because case is a query-global
  flag (parser ``_resolve_case``, last-wins), every content/regex leaf in a tree shares
  it, so we derive it from any such leaf and thread it to ``file:``/``sym:`` lowering.
  ``case_sensitive=None`` (default) derives-from-leaf (exact for any query with a
  content/regex term; insensitive fallback for a filter-only query). A caller holding
  the raw query may pass ``resolve_case(query)`` to make the filter-only ``case:yes``
  case exact. ``repo:`` is ALWAYS case-insensitive (``~*``) -- it is not in the
  case-flip set.
* Regex is opaque. Regex/filter patterns bind RAW as parameters -- never escaped, never
  ``re.compile``-d. An invalid POSIX ARE surfaces as a DB execution error at query time,
  not at compile time.
* ``lang:`` normalization. ``File.lang == lang.strip().lower()``; unknown values match
  nothing (empty result) rather than raising. No ``indexer`` import.
* Substring escaping. ``LIKE``/``ILIKE`` literals escape ``\\``, ``%``, ``_`` (backslash
  first) with ``escape="\\"``.
* Ordering / projection. Results project ``(id, repo_id, path, lang)`` -- NOT the
  up-to-MB nullable ``content`` (content is fetched per confirmed match). Ordered by
  ``(repo_id, path, content_sha)``, unique per ``UniqueConstraint(repo_id, path,
  content_sha)`` -> a deterministic, stable ``LIMIT`` page with no ``id`` tiebreak.
  Predicate emission order does NOT drive execution -- Postgres reorders ANDed predicates
  by its own statistics.
* Branch scoping. ``branch:<value>`` (:class:`BranchFilter`) lowers to the
  GIN-served exact-membership operator ``files.branches @> ARRAY[:v]``. When the AST carries
  NO ``BranchFilter`` anywhere, :func:`compile_query` ANDs in an IMPLICIT default-branch
  conjunct: a correlated ``EXISTS`` against ``repos`` testing
  ``coalesce(repos.default_branch, 'HEAD') = ANY(files.branches)``. That ``coalesce`` is
  byte-identical to the one used by the migration backfill and by the semantic default leg /
  ``get_file`` -- a NULL ``default_branch`` must resolve to ``'HEAD'`` everywhere. Unlike the
  explicit ``@>`` path, this default conjunct is a per-row correlated filter and is NOT
  GIN-served: it runs behind whatever trgm/content scan the rest of the predicate reaches.
"""

from __future__ import annotations

from typing import assert_never

from sqlalchemy import Select, Text, and_, any_, exists, func, literal, or_, select
from sqlalchemy.dialects.postgresql import ARRAY, array
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import File, Repo, RepoBranch, Symbol
from app.query.parser import (
    And,
    BranchFilter,
    CommitFilter,
    LangFilter,
    Node,
    Or,
    PathFilter,
    Regex,
    RepoFilter,
    Substring,
    SymbolFilter,
)

# Configurable row cap; module const per repo convention (cf. _MAX_DEPTH, MAX_FILE_BYTES).
DEFAULT_ROW_LIMIT = 200

# Search-relevant projection: id + locator columns, NOT the (nullable, up-to-MB) content.
_RESULT_COLUMNS = (File.id, File.repo_id, File.path, File.lang)


def _has_branch_filter(node: Node) -> bool:
    """True iff ``node`` (any And/Or nesting) contains a :class:`BranchFilter` leaf.

    Mirrors :func:`_global_case`'s tree walk. An explicit ``branch:`` anywhere in the query
    opts the whole query OUT of the implicit default-branch conjunct -- the query author has
    already stated which branch(es) they want.
    """
    match node:
        case BranchFilter() | CommitFilter():
            # A commit scope IS a branch scope (it resolves to specific repo/branch heads), so it
            # opts the query out of the implicit default-branch conjunct too -- without this a
            # commit resolving to a non-default branch would silently intersect to zero rows.
            return True
        case And(children=children) | Or(children=children):
            return any(_has_branch_filter(child) for child in children)
        case Substring() | Regex() | RepoFilter() | PathFilter() | LangFilter() | SymbolFilter():
            return False
        case _:
            assert_never(node)


def _default_branch_conjunct() -> ColumnElement[bool]:
    """The implicit default-branch predicate ANDed in when no ``branch:`` atom is present.

    A correlated ``EXISTS`` against ``repos`` (not a constant) so each file is checked
    against ITS OWN repo's default branch: ``coalesce(repos.default_branch, 'HEAD') =
    ANY(files.branches)``. The ``coalesce(..., 'HEAD')`` must stay byte-identical to the
    migration backfill and the semantic/``get_file`` default sites. NOT GIN-served.
    """
    return exists(
        select(Repo.id).where(
            Repo.id == File.repo_id,
            func.coalesce(Repo.default_branch, "HEAD") == any_(File.branches),
        )
    )


def compile_query(
    node: Node,
    *,
    limit: int = DEFAULT_ROW_LIMIT,
    case_sensitive: bool | None = None,
) -> Select:
    """Lower a parser AST into a SQLAlchemy Core ``select`` over ``files``.

    Pure: needs no DB connection. ``case_sensitive=None`` (default) derives the
    query-global case flag from any ``Substring``/``Regex`` leaf (exact for any query with
    a content/regex term; insensitive fallback for filter-only queries). Callers holding
    the raw query string may pass ``resolve_case(query)`` to make the filter-only
    ``case:yes`` case exact. Raises ``ValueError`` when ``limit`` is negative.

    Branch scoping: an explicit ``branch:`` atom anywhere in ``node`` lowers to
    ``files.branches @> ARRAY[:v]`` (GIN-served); when absent, the implicit correlated
    default-branch conjunct (see :func:`_default_branch_conjunct`) is ANDed in instead.
    """
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    cs = _global_case(node) if case_sensitive is None else case_sensitive
    predicate = _lower(node, cs)
    if not _has_branch_filter(node):
        predicate = and_(predicate, _default_branch_conjunct())
    return (
        select(*_RESULT_COLUMNS)
        .where(predicate)
        .order_by(File.repo_id, File.path, File.content_sha)
        .limit(limit)
    )


def _like_escape(value: str) -> str:
    """Escape a user literal for ``LIKE``/``ILIKE`` (backslash first, then ``%``/``_``)."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _global_case(node: Node) -> bool:
    """Derive the query-global case flag: True iff any Substring/Regex leaf is stamped."""
    match node:
        case Substring(case_sensitive=cs) | Regex(case_sensitive=cs):
            return cs
        case And(children=children) | Or(children=children):
            return any(_global_case(child) for child in children)
        case (
            RepoFilter()
            | PathFilter()
            | LangFilter()
            | SymbolFilter()
            | BranchFilter()
            | CommitFilter()
        ):
            return False
        case _:
            assert_never(node)


def _lower(node: Node, cs: bool) -> ColumnElement[bool]:
    """Lower a single AST node to a boolean SQL predicate. ``cs`` = global case flag."""
    match node:
        case Substring(value=value, case_sensitive=node_cs):
            pattern = f"%{_like_escape(value)}%"
            if node_cs:
                return File.content.like(pattern, escape="\\")
            return File.content.ilike(pattern, escape="\\")
        case Regex(pattern=pattern, case_sensitive=node_cs):
            op = "~" if node_cs else "~*"
            return File.content.op(op, is_comparison=True)(pattern)
        case RepoFilter(pattern=pattern):
            # repo is ALWAYS case-insensitive (not in the issue's case-flip list).
            return File.repo_id.in_(
                select(Repo.id).where(Repo.name.op("~*", is_comparison=True)(pattern))
            )
        case PathFilter(pattern=pattern):
            op = "~" if cs else "~*"
            return File.path.op(op, is_comparison=True)(pattern)
        case LangFilter(lang=lang):
            return File.lang == lang.strip().lower()
        case SymbolFilter(name=name):
            op = "~" if cs else "~*"
            return exists(
                select(Symbol.id).where(
                    and_(
                        Symbol.file_id == File.id,
                        Symbol.name.op(op, is_comparison=True)(name),
                    )
                )
            )
        case BranchFilter(value=v):
            # GIN-served exact membership -- explicit branch: opts out of the implicit
            # default-branch conjunct (see compile_query / _has_branch_filter).
            return File.branches.op("@>")(literal([v], type_=ARRAY(Text)))
        case CommitFilter(value=prefix):
            # Resolve the hex prefix through repo_branches (the ONLY commit truth-source; never
            # files.commit) to the (repo, branch) heads it names, and scope to files on those
            # branches: EXISTS a repo_branches row for THIS file's repo whose branch this file
            # carries and whose last_indexed_commit starts with the prefix. `literal(prefix)` is a
            # per-node auto-named bind, so `commit:a OR commit:b` never collide on one param name.
            return exists(
                select(RepoBranch.id).where(
                    RepoBranch.repo_id == File.repo_id,
                    File.branches.op("@>")(array([RepoBranch.branch])),
                    func.lower(RepoBranch.last_indexed_commit).like(
                        func.lower(literal(prefix)).concat("%")
                    ),
                )
            )
        case And(children=children):
            return and_(*[_lower(child, cs) for child in children])
        case Or(children=children):
            return or_(*[_lower(child, cs) for child in children])
        case _:
            assert_never(node)
