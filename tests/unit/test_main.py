"""Unit tests for the MCP server serialization + error mapping + observability (issue #11).

No DB, no SDK: the payload builders (``_search_code_payload`` / ``_list_repos_payload`` /
``_get_file_payload``) are driven with a fake engine/connection and a fake ``GrepResult`` so
each wire shape is pinned against the zoekt parity shapes asserted here (this module IS the
operative pin on the envelope contract). The ``_dispatch`` choke-point is exercised to
prove an unexpected fault is logged with a traceback and re-raised (never swallowed), and that
recoverable/saturation signals are logged.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pytest

from app import main
from app.config import Settings
from app.query.parser import QueryParseError
from app.search.errors import QueryTooBroadError
from app.search.grep import FileMatches, GrepResult, LineMatch
from app.search.symbols import SymbolMatch, SymbolResult


def _cfg() -> Settings:
    """A deterministic Settings instance (never reads the real environment)."""
    return Settings(
        lakebase_endpoint=None,
        statement_timeout_ms=5000,
        max_content_bytes=8 * 1024 * 1024,
        row_limit=200,
        max_row_limit=1000,
        semantic_enabled=False,
    )


# --------------------------------------------------------------------------- fake engine


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeConn:
    """Minimal Connection stand-in: records SQL and returns canned rows by call order."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.driver_sql: list[str] = []

    # engine.connect() context manager
    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    # conn.begin() context manager (transaction-local SET LOCAL)
    def begin(self) -> _FakeConn:
        return self

    def exec_driver_sql(self, sql: str) -> None:
        self.driver_sql.append(sql)

    def execute(self, *_args: object, **_kwargs: object) -> _FakeResult:
        return self._results.pop(0)


class _FakeEngine:
    def __init__(self, results: list[Any]) -> None:
        self._conn = _FakeConn(results)

    def connect(self) -> _FakeConn:
        return self._conn


class _Row:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


# ------------------------------------------------------------------- search_code shape


def _grep(
    *,
    files: tuple[FileMatches, ...] = (),
    truncated: bool = False,
    truncation_reason: str | None = None,
    regex_incompatible: bool = False,
    no_content_atom: bool = False,
    zero_width_only_atoms: bool = False,
) -> GrepResult:
    """Build a fake GrepResult, defaulting every field to its "nothing notable" value.

    ``GrepResult`` deliberately declares NO defaults so mypy catches a missed construction
    site in ``app/``. This test-only factory re-supplies them here so the next field addition
    touches one helper instead of every call site.
    """
    return GrepResult(
        files=files,
        truncated=truncated,
        truncation_reason=truncation_reason,
        regex_incompatible=regex_incompatible,
        no_content_atom=no_content_atom,
        zero_width_only_atoms=zero_width_only_atoms,
    )


def _grep_result() -> GrepResult:
    """A fake GrepResult mirroring the golden search fixture (one file, two byte ranges)."""
    return _grep(
        files=(
            FileMatches(
                repo_id=7,
                path="src/handler.go",
                lang="go",
                line_matches=(
                    LineMatch(
                        line_number=3,
                        line_text="// foo lives here and foo again",
                        byte_ranges=((3, 6), (24, 27)),
                    ),
                ),
            ),
        ),
    )


@pytest.mark.unit
def test_search_code_payload_matches_golden_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    # grep is stubbed; the builder only needs the repo_id->name SELECT after it.
    monkeypatch.setattr(main, "grep_search", lambda *a, **k: _grep_result())
    engine = _FakeEngine([_FakeResult([_Row(id=7, name="acme/widgets")])])

    payload = main._search_code_payload(engine, _cfg(), "foo", 50)

    assert payload["query"] == "foo"
    assert payload["file_count"] == 1
    assert payload["match_count"] == 2
    assert isinstance(payload["duration_ns"], int)
    assert payload["truncated"] is False
    assert payload["truncation_reason"] is None
    assert payload["regex_incompatible"] is False
    assert payload["query_too_broad"] is False
    assert payload["query_parse_error"] is None
    # An ordinary content query proves nothing about the query shape: both flags stay False.
    assert payload["no_content_atom"] is False
    assert payload["zero_width_only_atoms"] is False
    assert payload["files"] == [
        {
            "repo": "acme/widgets",
            "file": "src/handler.go",
            "language": "go",
            "branches": ["HEAD"],
            "matches": [
                {
                    "line": 3,
                    "text": "// foo lives here and foo again",
                    "byte_ranges": [[3, 6], [24, 27]],
                }
            ],
        }
    ]


@pytest.mark.unit
def test_search_code_query_parse_error_maps_to_field(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: object, **_k: object) -> GrepResult:
        raise QueryParseError("bad query", 0)

    monkeypatch.setattr(main, "grep_search", _raise)
    payload = main._search_code_payload(_FakeEngine([]), _cfg(), "case:maybe", 50)

    assert payload["query_parse_error"] == "bad query"
    assert payload["files"] == []
    assert payload["file_count"] == 0
    assert payload["match_count"] == 0
    assert payload["query_too_broad"] is False
    # An unparseable query was never classified, so neither shape flag can be proven. [AC4]
    assert payload["no_content_atom"] is False
    assert payload["zero_width_only_atoms"] is False


@pytest.mark.unit
def test_search_code_query_too_broad_maps_to_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: object, **_k: object) -> GrepResult:
        raise QueryTooBroadError("too broad")

    monkeypatch.setattr(main, "grep_search", _raise)
    payload = main._search_code_payload(_FakeEngine([]), _cfg(), "foo", 50)

    assert payload["query_too_broad"] is True
    assert payload["truncated"] is True
    assert payload["files"] == []
    assert payload["query_parse_error"] is None
    # grep never returned, so there is no shape fact to report. [AC4]
    assert payload["no_content_atom"] is False
    assert payload["zero_width_only_atoms"] is False


@pytest.mark.unit
def test_search_code_truncation_and_regex_incompatible_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # regex_incompatible=True with both new flags False is the shape both grep helpers
    # guarantee: an uncompilable atom is a content atom of UNKNOWN width, never a proof.
    result = _grep(truncated=True, truncation_reason="byte_cap", regex_incompatible=True)
    monkeypatch.setattr(main, "grep_search", lambda *a, **k: result)
    engine = _FakeEngine([_FakeResult([])])  # empty repo-name map

    payload = main._search_code_payload(engine, _cfg(), "foo", 50)
    assert payload["truncated"] is True
    assert payload["truncation_reason"] == "byte_cap"
    assert payload["regex_incompatible"] is True
    assert payload["files"] == []
    assert payload["no_content_atom"] is False
    assert payload["zero_width_only_atoms"] is False


# ----------------------------------------------------------------- sym: fold into search_code


def _sym_result(*matches: SymbolMatch, truncated: bool = False) -> SymbolResult:
    return SymbolResult(
        symbols=tuple(matches),
        truncated=truncated,
        truncation_reason="row_cap" if truncated else None,
        no_symbol_atom=False,
    )


@pytest.mark.unit
def test_sym_matches_merge_into_same_file_ordered_by_line(monkeypatch: pytest.MonkeyPatch) -> None:
    # grep finds a content match on line 3; symbol search finds a Handler def on line 2 of the
    # SAME file. They fold into one file entry, matches ordered by line (symbol first).
    monkeypatch.setattr(main, "grep_search", lambda *a, **k: _grep_result())
    monkeypatch.setattr(
        main,
        "symbol_search",
        lambda *a, **k: _sym_result(
            SymbolMatch(
                repo_id=7,
                path="src/handler.go",
                lang="go",
                name="Handler",
                kind="function",
                start_line=2,
            )
        ),
    )
    engine = _FakeEngine([_FakeResult([_Row(id=7, name="acme/widgets")])])

    payload = main._search_code_payload(engine, _cfg(), "sym:Handler foo", 50)

    assert payload["file_count"] == 1
    # 2 content spans + 1 symbol definition.
    assert payload["match_count"] == 3
    (file,) = payload["files"]
    assert file["repo"] == "acme/widgets"
    assert file["matches"] == [
        {
            "line": 2,
            "text": "",
            "byte_ranges": [],
            "symbols": [{"name": "Handler", "kind": "function"}],
        },
        {
            "line": 3,
            "text": "// foo lives here and foo again",
            "byte_ranges": [[3, 6], [24, 27]],
        },
    ]


@pytest.mark.unit
def test_sym_only_query_returns_symbol_file_grep_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # A sym:-only query: grep is highlight-driven and returns nothing; the symbol leg carries it.
    # A sym:-only query IS filter-only at the grep layer, so grep reports no_content_atom=True;
    # the envelope must suppress it because the symbol leg answers. Live assertion below.
    empty_grep = _grep(no_content_atom=True)
    monkeypatch.setattr(main, "grep_search", lambda *a, **k: empty_grep)
    monkeypatch.setattr(
        main,
        "symbol_search",
        lambda *a, **k: _sym_result(
            SymbolMatch(
                repo_id=7,
                path="src/handler.go",
                lang="go",
                name="Handler",
                kind="function",
                start_line=2,
            )
        ),
    )
    engine = _FakeEngine([_FakeResult([_Row(id=7, name="acme/widgets")])])

    payload = main._search_code_payload(engine, _cfg(), "sym:Handler", 50)

    assert payload["file_count"] == 1
    assert payload["match_count"] == 1
    (file,) = payload["files"]
    assert file["file"] == "src/handler.go"
    assert file["matches"][0]["symbols"] == [{"name": "Handler", "kind": "function"}]
    assert payload["query_too_broad"] is False
    # Suppressed: the symbol leg answered, so flagging the shape would contradict file_count.
    assert payload["no_content_atom"] is False


@pytest.mark.unit
def test_sym_leg_timeout_flags_query_too_broad_keeps_grep(monkeypatch: pytest.MonkeyPatch) -> None:
    # grep succeeds but the symbol leg times out: flag query_too_broad + truncated, keep content.
    monkeypatch.setattr(main, "grep_search", lambda *a, **k: _grep_result())

    def _raise(*_a: object, **_k: object) -> SymbolResult:
        raise QueryTooBroadError("symbol leg too broad")

    monkeypatch.setattr(main, "symbol_search", _raise)
    engine = _FakeEngine([_FakeResult([_Row(id=7, name="acme/widgets")])])

    payload = main._search_code_payload(engine, _cfg(), "sym:Handler foo", 50)

    assert payload["query_too_broad"] is True
    assert payload["truncated"] is True
    assert payload["file_count"] == 1  # grep's content match is still returned
    assert payload["match_count"] == 2


@pytest.mark.unit
def test_sym_truncation_sets_row_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # A sym:-only query IS filter-only at the grep layer, so grep reports no_content_atom=True;
    # the envelope must suppress it because the symbol leg answers. Live assertion below.
    empty_grep = _grep(no_content_atom=True)
    monkeypatch.setattr(main, "grep_search", lambda *a, **k: empty_grep)
    monkeypatch.setattr(
        main,
        "symbol_search",
        lambda *a, **k: _sym_result(
            SymbolMatch(
                repo_id=7,
                path="src/handler.go",
                lang="go",
                name="Handler",
                kind="function",
                start_line=2,
            ),
            truncated=True,
        ),
    )
    engine = _FakeEngine([_FakeResult([_Row(id=7, name="acme/widgets")])])

    payload = main._search_code_payload(engine, _cfg(), "sym:Handler", 50)
    assert payload["truncated"] is True
    assert payload["truncation_reason"] == "row_cap"


# ------------------------------------------------------ query-shape flags on the envelope (#31)
#
# The envelope's job is suppression: grep's raw per-leg fact ANDed with "the symbol leg did
# not answer this query". These pin both directions of that AND.


def _no_sym() -> SymbolResult:
    """A symbol leg that structurally cannot answer: the query carried no sym: atom."""
    return SymbolResult(symbols=(), truncated=False, truncation_reason=None, no_symbol_atom=True)


def _one_sym() -> SymbolResult:
    """A symbol leg that DID answer, with one definition match."""
    return _sym_result(
        SymbolMatch(
            repo_id=7,
            path="src/handler.go",
            lang="go",
            name="Handler",
            kind="function",
            start_line=2,
        )
    )


@pytest.mark.unit
def test_filter_only_grep_sets_no_content_atom_on_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    # The reported bug (AC1): `file:.md` returns zero files and the agent cannot tell that
    # from "no .md file contains anything". Now it can.
    monkeypatch.setattr(main, "grep_search", lambda *a, **k: _grep(no_content_atom=True))
    monkeypatch.setattr(main, "symbol_search", lambda *a, **k: _no_sym())

    payload = main._search_code_payload(_FakeEngine([]), _cfg(), "file:.md", 50)

    assert payload["no_content_atom"] is True
    assert payload["zero_width_only_atoms"] is False
    assert payload["file_count"] == 0
    assert payload["query_parse_error"] is None
    assert payload["query_too_broad"] is False


@pytest.mark.unit
def test_genuine_zero_match_does_not_set_no_content_atom(monkeypatch: pytest.MonkeyPatch) -> None:
    # The other half of AC1: an ordinary true negative reaches the SAME file_count of 0 with
    # the flag False. The pair is what makes the signal informative.
    monkeypatch.setattr(main, "grep_search", lambda *a, **k: _grep())
    monkeypatch.setattr(main, "symbol_search", lambda *a, **k: _no_sym())

    payload = main._search_code_payload(_FakeEngine([]), _cfg(), "zzznotpresentzzz", 50)

    assert payload["no_content_atom"] is False
    assert payload["file_count"] == 0


@pytest.mark.unit
def test_sym_only_query_does_not_set_no_content_atom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "grep_search", lambda *a, **k: _grep(no_content_atom=True))
    monkeypatch.setattr(main, "symbol_search", lambda *a, **k: _one_sym())
    engine = _FakeEngine([_FakeResult([_Row(id=7, name="acme/widgets")])])

    payload = main._search_code_payload(engine, _cfg(), "sym:Handler", 50)

    assert payload["no_content_atom"] is False
    assert payload["file_count"] == 1


@pytest.mark.unit
def test_sym_leg_timeout_suppresses_no_content_atom(monkeypatch: pytest.MonkeyPatch) -> None:
    # A timed-out symbol leg yields sym_result is None, which is PROVABLY the sym-bearing
    # shape (only a DB hit can time out, and the leg short-circuits before the DB when there
    # is no sym: atom). The inverted `is not None and ...` form emits a false flag here.
    monkeypatch.setattr(main, "grep_search", lambda *a, **k: _grep(no_content_atom=True))

    def _raise(*_a: object, **_k: object) -> SymbolResult:
        raise QueryTooBroadError("symbol leg too broad")

    monkeypatch.setattr(main, "symbol_search", _raise)

    payload = main._search_code_payload(_FakeEngine([]), _cfg(), "sym:Handler", 50)

    assert payload["no_content_atom"] is False
    assert payload["query_too_broad"] is True


@pytest.mark.unit
def test_zero_width_with_sym_answer_is_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    # The `sym:Handler /^/` shape: zero-width-only to grep, yet the symbol leg folds real
    # matches into files. Flagging a query that RETURNED RESULTS is the exact failure mode
    # suppression exists to prevent -- and it is what keeps grep's "flag implies files empty"
    # invariant true at this layer too.
    monkeypatch.setattr(main, "grep_search", lambda *a, **k: _grep(zero_width_only_atoms=True))
    monkeypatch.setattr(main, "symbol_search", lambda *a, **k: _one_sym())
    engine = _FakeEngine([_FakeResult([_Row(id=7, name="acme/widgets")])])

    payload = main._search_code_payload(engine, _cfg(), "sym:Handler /^/", 50)

    assert payload["zero_width_only_atoms"] is False
    assert payload["file_count"] == 1


@pytest.mark.unit
def test_zero_width_query_sets_flag_without_regex_incompatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC3: `/^/` compiled fine, so regex_incompatible stays False -- the two signals are
    # distinct conditions and the envelope mirrors both faithfully.
    monkeypatch.setattr(
        main,
        "grep_search",
        lambda *a, **k: _grep(zero_width_only_atoms=True, regex_incompatible=False),
    )
    monkeypatch.setattr(main, "symbol_search", lambda *a, **k: _no_sym())

    payload = main._search_code_payload(_FakeEngine([]), _cfg(), "/^/", 50)

    assert payload["zero_width_only_atoms"] is True
    assert payload["regex_incompatible"] is False
    assert payload["no_content_atom"] is False
    assert payload["file_count"] == 0


@pytest.mark.unit
def test_envelope_keys_are_pinned_shape_plus_exactly_two(monkeypatch: pytest.MonkeyPatch) -> None:
    # The envelope is additive-only and permanent: pin that this change added the two named
    # keys and NOTHING else, and removed nothing.
    monkeypatch.setattr(main, "grep_search", lambda *a, **k: _grep())
    monkeypatch.setattr(main, "symbol_search", lambda *a, **k: _no_sym())

    payload = main._search_code_payload(_FakeEngine([]), _cfg(), "foo", 50)

    pinned = {
        "query",
        "file_count",
        "match_count",
        "duration_ns",
        "files",
        "truncated",
        "truncation_reason",
        "regex_incompatible",
        "query_too_broad",
        "query_parse_error",
    }
    assert pinned <= set(payload)
    assert set(payload) - pinned == {"no_content_atom", "zero_width_only_atoms"}


# ---------------------------------------------------------------------- list_repos shape


@pytest.mark.unit
def test_list_repos_payload_shape_and_iso8601() -> None:
    rows = [
        _Row(
            name="acme/widgets",
            default_branch="main",
            last_indexed_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
            last_indexed_commit="abc123",
        ),
        _Row(
            name="beta/tools", default_branch=None, last_indexed_at=None, last_indexed_commit=None
        ),
    ]
    engine = _FakeEngine([_FakeResult(rows)])

    payload = main._list_repos_payload(engine, _cfg())
    assert payload["count"] == 2
    assert payload["repos"][0] == {
        "name": "acme/widgets",
        "branches": ["main"],
        "index_time": "2026-07-18T00:00:00+00:00",
        "default_branch": "main",
        "last_indexed_commit": "abc123",
    }
    # Null branch -> ["HEAD"]; null index time -> null.
    assert payload["repos"][1]["branches"] == ["HEAD"]
    assert payload["repos"][1]["index_time"] is None


@pytest.mark.unit
def test_list_repos_sets_transaction_local_timeout() -> None:
    engine = _FakeEngine([_FakeResult([])])
    main._list_repos_payload(engine, _cfg())
    assert engine._conn.driver_sql == ["SET LOCAL statement_timeout = 5000"]


# ------------------------------------------------------------------------ get_file shape


@pytest.mark.unit
def test_get_file_hit_shape() -> None:
    engine = _FakeEngine([_FakeResult(["package main\n..."])])
    payload = main._get_file_payload(engine, _cfg(), "acme/widgets", "src/handler.go")
    assert payload == {
        "repo": "acme/widgets",
        "path": "src/handler.go",
        "branch": "HEAD",
        "content": "package main\n...",
        "found": True,
    }


@pytest.mark.unit
def test_get_file_miss_shape() -> None:
    engine = _FakeEngine([_FakeResult([])])  # scalar_one_or_none -> None
    payload = main._get_file_payload(engine, _cfg(), "acme/widgets", "nope.go")
    assert payload == {
        "repo": "acme/widgets",
        "path": "nope.go",
        "branch": "HEAD",
        "content": None,
        "found": False,
    }


# ------------------------------------------------------------------------------- clamp


@pytest.mark.unit
@pytest.mark.parametrize(
    "limit,expected",
    [(0, 200), (-5, 200), (10_000, 1000), (50, 50), (1000, 1000), (1, 1)],
)
def test_clamp_limit(limit: int, expected: int) -> None:
    assert main._clamp_limit(limit, _cfg()) == expected


# ------------------------------------------------------------- observability choke-point


@pytest.mark.observability
@pytest.mark.asyncio
async def test_dispatch_reraises_and_logs_unexpected_fault(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _boom() -> dict[str, Any]:
        raise RuntimeError("kaboom")

    with caplog.at_level(logging.ERROR, logger="app.tools"):
        with pytest.raises(RuntimeError, match="kaboom"):
            await main._dispatch("search_code", _boom)

    # The fault is logged with a full traceback (exc_info present), NOT swallowed.
    records = [r for r in caplog.records if r.name == "app.tools" and r.levelno == logging.ERROR]
    assert records, "expected an error log record from the choke-point"
    assert any("tool=search_code failed" in r.getMessage() for r in records)
    assert any(r.exc_info is not None for r in records)


@pytest.mark.observability
@pytest.mark.asyncio
async def test_dispatch_logs_signals_and_saturation(caplog: pytest.LogCaptureFixture) -> None:
    def _build() -> dict[str, Any]:
        return {"query_too_broad": True, "truncated": True, "query_parse_error": None}

    with caplog.at_level(logging.INFO, logger="app.tools"):
        out = await main._dispatch("search_code", _build)

    # Returns the json.dumps'd payload.
    assert '"query_too_broad": true' in out
    line = next(r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
    assert "tool=search_code" in line
    assert "query_too_broad" in line
    assert "limiter_borrowed=" in line  # pool/limiter saturation signal is wired


@pytest.mark.observability
def test_signals_log_includes_both_flags() -> None:
    # Read straight off the payload dict, so a filter-only query is diagnosable from the logs
    # alone rather than looking identical to a genuine no-match.
    signals = main._signals({"no_content_atom": True, "zero_width_only_atoms": False})
    assert signals["no_content_atom"] is True
    assert signals["zero_width_only_atoms"] is False
