"""Route-level unit tests for the webui FastAPI backend (issue #35 WS-B).

No DB, no SDK, no real Lakebase engine: ``get_engine``/``get_settings`` are FastAPI
dependencies (``webui/main.py``), so tests override them (``app.dependency_overrides``) with a
fake engine/connection and a deterministic ``Settings``, then drive requests through a real
``TestClient`` -- pinning the actual HTTP wire shape (status codes, JSON bodies), not just the
underlying payload-builder dicts (those are already pinned by ``tests/unit/test_main.py``).
``service.grep_search``/``service.symbol_search`` are monkeypatched the same way
``test_main.py`` does it, since ``app.service.search_code_payload`` resolves those names from
its OWN module globals regardless of which app calls it.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import service
from app.config import Settings
from app.query.parser import QueryParseError
from app.search.grep import FileCursor, FileMatches, GrepResult, LineMatch
from webui.main import app, get_engine, get_settings


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

    def __init__(self, results: list[Any], *, raise_on_driver_sql: bool = False) -> None:
        self._results = list(results)
        self._raise_on_driver_sql = raise_on_driver_sql
        self.driver_sql: list[str] = []

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def begin(self) -> _FakeConn:
        return self

    def exec_driver_sql(self, sql: str) -> None:
        if self._raise_on_driver_sql:
            raise RuntimeError("connection refused")
        self.driver_sql.append(sql)

    def execute(self, *_args: object, **_kwargs: object) -> _FakeResult:
        return self._results.pop(0)


class _FakeEngine:
    def __init__(
        self, results: list[Any] | None = None, *, raise_on_driver_sql: bool = False
    ) -> None:
        self._conn = _FakeConn(results or [], raise_on_driver_sql=raise_on_driver_sql)

    def connect(self) -> _FakeConn:
        return self._conn


class _Row:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def _grep_result(*, next_cursor: FileCursor | None = None) -> GrepResult:
    """A fake GrepResult mirroring the golden search fixture (one file, one byte range)."""
    return GrepResult(
        files=(
            FileMatches(
                repo_id=7,
                path="src/handler.go",
                lang="go",
                line_matches=(
                    LineMatch(line_number=3, line_text="foo lives here", byte_ranges=((0, 3),)),
                ),
            ),
        ),
        truncated=False,
        truncation_reason=None,
        regex_incompatible=False,
        no_content_atom=False,
        zero_width_only_atoms=False,
        next_cursor=next_cursor,
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    """A TestClient with the engine/settings dependencies overridden to fakes."""
    engine = _FakeEngine([_FakeResult([_Row(id=7, name="acme/widgets")])])
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_settings] = _cfg
    monkeypatch.setattr(service, "symbol_search", lambda *a, **k: None)
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


# ------------------------------------------------------------------------------------ /health


@pytest.mark.unit
def test_health_is_always_ok() -> None:
    # No dependency overrides needed: health touches neither engine nor settings.
    with TestClient(app) as test_client:
        resp = test_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ------------------------------------------------------------------------------------- /ready


@pytest.mark.unit
def test_ready_returns_ready_on_success() -> None:
    engine = _FakeEngine()
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_settings] = _cfg
    try:
        with TestClient(app) as test_client:
            resp = test_client.get("/ready")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


@pytest.mark.unit
def test_ready_returns_503_when_probe_fails() -> None:
    engine = _FakeEngine(raise_on_driver_sql=True)
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_settings] = _cfg
    try:
        with TestClient(app) as test_client:
            resp = test_client.get("/ready")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 503
    assert resp.json() == {"status": "unready"}


# --------------------------------------------------------------------------------- /api/search


@pytest.mark.unit
def test_api_search_wire_shape_includes_next_cursor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    cursor = FileCursor(repo_id=7, path="src/handler.go")
    monkeypatch.setattr(service, "grep_search", lambda *a, **k: _grep_result(next_cursor=cursor))

    resp = client.get("/api/search", params={"q": "foo"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "foo"
    assert body["file_count"] == 1
    assert body["files"][0]["repo"] == "acme/widgets"
    # Pagination mode (the webui API always supplies cursor=) always carries next_cursor,
    # unlike the bare MCP-tool envelope which omits the key entirely.
    assert "next_cursor" in body
    assert isinstance(body["next_cursor"], str)


@pytest.mark.unit
def test_api_search_exhausted_page_has_null_next_cursor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "grep_search", lambda *a, **k: _grep_result(next_cursor=None))

    resp = client.get("/api/search", params={"q": "foo"})

    assert resp.status_code == 200
    assert resp.json()["next_cursor"] is None


@pytest.mark.unit
def test_api_search_query_parse_error_is_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # service.search_code_payload catches QueryParseError INTERNALLY and folds it into the
    # payload's query_parse_error field (never raises it out -- see webui/main.py:api_search's
    # docstring), so the route's 400 mapping is driven by that stubbed grep_search raising it.
    def _raise(*_a: object, **_k: object) -> GrepResult:
        raise QueryParseError("bad query", 3)

    monkeypatch.setattr(service, "grep_search", _raise)

    resp = client.get("/api/search", params={"q": "case:maybe"})

    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "bad query"


@pytest.mark.unit
def test_api_search_bad_cursor_is_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "grep_search", lambda *a, **k: _grep_result())

    resp = client.get("/api/search", params={"q": "foo", "cursor": "not-a-valid-cursor!!"})

    assert resp.status_code == 400
    assert "error" in resp.json()["detail"]


# ----------------------------------------------------------------------------------- /api/file


@pytest.mark.unit
def test_api_file_found(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _FakeEngine([_FakeResult(["print('hi')\n"])])
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_settings] = _cfg
    try:
        with TestClient(app) as test_client:
            resp = test_client.get("/api/file", params={"repo": "acme/widgets", "path": "a.py"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["content"] == "print('hi')\n"
    assert body["branch"] == "HEAD"


@pytest.mark.unit
def test_api_file_missing_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _FakeEngine([_FakeResult([])])  # scalar_one_or_none() -> None
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_settings] = _cfg
    try:
        with TestClient(app) as test_client:
            resp = test_client.get(
                "/api/file", params={"repo": "acme/widgets", "path": "missing.py"}
            )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404
    body = resp.json()["detail"]
    assert body["repo"] == "acme/widgets"
    assert body["path"] == "missing.py"


# ---------------------------------------------------------------------------------- /api/repos


@pytest.mark.unit
def test_api_repos_lists_indexed_repos() -> None:
    engine = _FakeEngine(
        [
            _FakeResult(
                [
                    _Row(
                        name="acme/widgets",
                        default_branch="main",
                        last_indexed_at=None,
                        last_indexed_commit="deadbeef",
                    )
                ]
            )
        ]
    )
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_settings] = _cfg
    try:
        with TestClient(app) as test_client:
            resp = test_client.get("/api/repos")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["repos"][0]["name"] == "acme/widgets"
    assert body["repos"][0]["last_indexed_commit"] == "deadbeef"
