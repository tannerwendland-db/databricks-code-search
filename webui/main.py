"""FastAPI web UI backend: a second Databricks App over the same corpus (issue #35).

Sibling to ``app/main.py`` (the MCP server): imports the SAME payload builders from
``app.service`` so search/file/repo-listing behavior is exactly the MCP tools' behavior,
just wired to HTTP GET routes and a submit-based frontend instead of MCP tool calls.

Deliberately owns its OWN engine singleton, ``CapacityLimiter``, and off-loop dispatch
pattern (copied from ``app/main.py:68-147``) rather than importing ``app.main``'s
module-globals: the two apps are independent Databricks Apps processes, each with its own DB
connection pool sized to its own capacity limiter, and importing ``app.main`` would build the
MCP ``FastMCP``/Starlette app as an unwanted import side effect (``app/main.py``'s module-level
``app = create_app()``).

``get_engine``/``get_settings`` are used as FastAPI dependencies (``Depends(...)``) rather than
being called directly from route bodies, so tests can override them
(``app.dependency_overrides[...]``) with a fake engine/deterministic settings without touching
the real Lakebase connection or environment -- the same test seam ``app/main.py``'s tests get
via the lifespan-context dict, adapted to FastAPI's own DI mechanism.
"""

from __future__ import annotations

import atexit
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import anyio
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Engine
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from app import service
from app.config import Settings, get_settings
from app.db.client import create_db_engine
from app.service import CursorError

logger = logging.getLogger("webui")

# Sized to app.db.client._DEFAULT_POOL_SIZE (5), which this process takes because it passes no
# pool_size of its own to create_db_engine -- see the identical reasoning in app/main.py. Bounds
# in-flight blocking calls to the single process-wide connection pool this app owns.
_DB_POOL_SIZE = 5
_DB_LIMITER = anyio.CapacityLimiter(_DB_POOL_SIZE)

_FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"


# --------------------------------------------------------------------- engine singleton


_engine: Engine | None = None
_engine_lock = threading.Lock()


def get_engine() -> Engine:
    """Return this app's process-scoped engine, building it once (lazily, race-safe).

    Copied from ``app/main.py:get_engine`` verbatim in spirit: a lazy, ``threading.Lock``
    -guarded module singleton (never built inside a FastAPI ``lifespan``, since the first
    ``create_db_engine()`` round-trips Lakebase and would otherwise stall startup or, worse,
    re-pay the cold start per worker), disposed once at process shutdown via ``atexit``.
    """
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                cfg = get_settings()
                engine = create_db_engine(endpoint=cfg.lakebase_endpoint)
                atexit.register(engine.dispose)
                _engine = engine
    return _engine


async def _run_blocking(fn: Callable[[], Any]) -> Any:
    """Await a blocking ``fn`` on a worker thread, bounded by the pool-sized limiter."""
    return await anyio.to_thread.run_sync(fn, limiter=_DB_LIMITER)


EngineDep = Annotated[Engine, Depends(get_engine)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


# ------------------------------------------------------------------------------- static SPA


class SPAStaticFiles(StaticFiles):
    """Serve the built SPA, falling back to ``index.html`` for any unmatched path.

    Client-side routes (e.g. ``/file?repo=x&path=y``) are not real files on disk; without this
    override, a hard refresh or a direct deep link would 404 instead of loading the SPA shell
    and letting ``src/router.ts`` hydrate from ``location``. This is mounted at ``/`` AFTER all
    API routers in :func:`create_app`, so ``/api/*``/``/health``/``/ready`` are matched first
    and never reach here.
    """

    async def get_response(self, path: str, scope: Scope) -> Any:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


# ------------------------------------------------------------------------------------ routes


async def health() -> dict[str, str]:
    """Liveness: zero-DB, always 200 while the process is up."""
    return {"status": "ok"}


async def ready(engine: EngineDep, cfg: SettingsDep) -> JSONResponse:
    """Readiness: a bounded probe against a real protected table.

    ``SELECT 1 FROM repos LIMIT 1`` (not a bare ``SELECT 1``) forces the SELECT grant check, so
    a ``CAN_CONNECT``-only role with no reader grant -- or an unreachable Lakebase -- surfaces
    as 503 instead of shipping green. Mirrors ``app/main.py:ready``.
    """

    def probe() -> None:
        with engine.connect() as conn:
            with conn.begin():
                timeout_ms = int(cfg.statement_timeout_ms)
                conn.exec_driver_sql(f"SET LOCAL statement_timeout = {timeout_ms}")
                conn.exec_driver_sql("SELECT 1 FROM repos LIMIT 1")

    try:
        await _run_blocking(probe)
        return JSONResponse({"status": "ready"})
    except Exception as error:
        # Log the detail server-side; return a generic body so an unauthenticated probe caller
        # never sees the raw DB error (which can echo the Lakebase host/schema/relation names).
        logger.warning("readiness probe failed: %r", error)
        return JSONResponse({"status": "unready"}, status_code=503)


async def api_search(
    engine: EngineDep,
    cfg: SettingsDep,
    q: Annotated[str, Query(min_length=1)],
    limit: Annotated[int, Query()] = 0,
    cursor: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Search the indexed corpus. Always pagination mode: ``cursor`` is passed explicitly
    (``None`` for page 1), so the response always carries ``next_cursor`` (issue #35 A2).

    Unlike :class:`app.service.CursorError` (raised uncaught -- see the ``except`` below),
    ``QueryParseError`` never escapes :func:`service.search_code_payload`: it is caught
    INSIDE the builder and folded into the returned payload's ``query_parse_error`` field
    (a structured signal, matching the MCP tools' "recoverable conditions are payload
    fields, never exceptions" contract -- see ``app/main.py``'s module docstring). So the
    400 mapping here inspects the payload rather than catching an exception.
    """
    clamped = service.clamp_limit(limit, cfg)
    try:
        payload = await _run_blocking(
            lambda: service.search_code_payload(engine, cfg, q, clamped, cursor=cursor)
        )
    except CursorError as error:
        raise HTTPException(status_code=400, detail={"error": str(error)}) from error
    if payload["query_parse_error"] is not None:
        raise HTTPException(status_code=400, detail={"error": payload["query_parse_error"]})
    return payload


async def api_file(
    engine: EngineDep,
    cfg: SettingsDep,
    repo: Annotated[str, Query(min_length=1)],
    path: Annotated[str, Query(min_length=1)],
) -> dict[str, Any]:
    """Return one file's full content by (repo name, path); a miss is a 404."""
    payload = await _run_blocking(lambda: service.get_file_payload(engine, cfg, repo, path))
    if not payload["found"]:
        detail = {"error": f"{repo}/{path} not found", "repo": repo, "path": path}
        raise HTTPException(status_code=404, detail=detail)
    return payload


async def api_repos(engine: EngineDep, cfg: SettingsDep) -> dict[str, Any]:
    """List every indexed repository with its branch and last-indexed metadata."""
    return await _run_blocking(lambda: service.list_repos_payload(engine, cfg))


# --------------------------------------------------------------------------- ASGI export


def create_app() -> FastAPI:
    """Build the FastAPI app: API routers first, then the SPA mount (so routes win)."""
    app = FastAPI(title="code-search webui")

    app.get("/health")(health)
    app.get("/ready")(ready)
    app.get("/api/search")(api_search)
    app.get("/api/file")(api_file)
    app.get("/api/repos")(api_repos)

    if _FRONTEND_DIST.is_dir():
        app.mount("/", SPAStaticFiles(directory=_FRONTEND_DIST, html=True), name="spa")
    else:
        logger.warning(
            "frontend dist directory not found at %s; run `make webui-build`. "
            "API routes are still served; the SPA is not.",
            _FRONTEND_DIST,
        )

    return app


app = create_app()
