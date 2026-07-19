"""Post-deploy smoke test for the code-search MCP app (issue #12).

Proves a *real* result after ``make deploy``, never a false green. Each probe has an
explicit meaning (see the grant-oracle table in ``.omc/plans/issue-12-deploy-plan.md``):

* ``GET /health`` == 200  -> liveness (ALWAYS).
* ``GET /ready``  == 200  -> the app SP's SELECT grant landed; this is the always-on
  **grant oracle** (it runs as the app SP against a protected table, 0-row-safe). Direct-SQL
  as the owner cannot detect a missing app grant, so ``/ready`` — not direct-SQL — is the
  honest oracle.
* direct-SQL ``SELECT 1`` as the developer/owner -> connectivity only, NOT a grant assertion.
* direct-SQL ``SELECT count(*) FROM repos >= 1`` -> corpus populated; only under
  ``--expect-indexed`` (a fresh deploy has an empty corpus, so this is opt-in and must not
  false-RED).
* MCP ``search_code`` -> end-to-end query returns file-grouped matches with line numbers; only
  under ``--enable-mcp`` when M2M client creds are present, else a printed manual step.

The pure predicate functions (``health_ok`` .. ``manual_step_message``) do no I/O and carry no
heavy imports, so they are unit-testable without a live app; the live legs import ``httpx`` /
``mcp`` / ``sqlalchemy`` lazily inside their functions.

Exit contract: non-zero if ``/health`` or ``/ready`` fail, if connectivity fails, if
``--expect-indexed`` and the corpus is empty, or if ``--enable-mcp`` is set and the MCP leg
fails. Zero (with an ``MCP leg = manual`` note) when M2M is not enabled. Never a silent green.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from typing import Any, NamedTuple


class Result(NamedTuple):
    """A single leg's outcome: ``ok`` gates the exit code, ``detail`` is human-facing."""

    ok: bool
    detail: str


# --------------------------------------------------------------------- pure predicates
#
# No I/O, stdlib-only imports: importable and unit-testable without a live app.


def health_ok(body: dict[str, Any]) -> bool:
    """Liveness predicate: the ``/health`` body reports ``status == "ok"``."""
    return body.get("status") == "ok"


def ready_ok(status_code: int) -> bool:
    """Grant-oracle predicate: ``/ready`` returned 200 (the app SP SELECT grant landed)."""
    return status_code == 200


def assert_connectivity(scalar: Any) -> Result:
    """A returned ``SELECT 1`` scalar proves connectivity — explicitly NOT a grant assertion."""
    ok = scalar == 1
    if ok:
        return Result(True, "connectivity: SELECT 1 returned 1 (owner; not a grant check)")
    return Result(False, f"connectivity: SELECT 1 returned {scalar!r}, expected 1")


def assert_corpus_nonempty(count: int) -> Result:
    """Corpus predicate (only under ``--expect-indexed``): ``count >= 1``."""
    ok = count >= 1
    if ok:
        return Result(True, f"corpus: repos count = {count} (>= 1)")
    return Result(False, "corpus: repos count = 0 (expected >= 1 under --expect-indexed)")


def validate_search_payload(payload: dict[str, Any]) -> Result:
    """Assert the pinned ``search_code`` envelope: ``file_count > 0``, each file has ``matches``,
    each match carries an integer ``line`` and a ``byte_ranges`` list (zoekt parity shape)."""
    file_count = payload.get("file_count")
    if not isinstance(file_count, int) or file_count <= 0:
        return Result(False, f"search_code: file_count = {file_count!r}, expected a positive int")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        return Result(False, "search_code: 'files' is empty or not a list")
    for fi, f in enumerate(files):
        matches = f.get("matches") if isinstance(f, dict) else None
        if not isinstance(matches, list) or not matches:
            return Result(False, f"search_code: files[{fi}].matches is empty or not a list")
        for mi, m in enumerate(matches):
            if not isinstance(m, dict) or not isinstance(m.get("line"), int):
                return Result(False, f"search_code: files[{fi}].matches[{mi}].line is not an int")
            if not isinstance(m.get("byte_ranges"), list):
                return Result(
                    False, f"search_code: files[{fi}].matches[{mi}].byte_ranges is not a list"
                )
    return Result(True, f"search_code: {file_count} file(s) with well-formed matches")


def m2m_available(env: Mapping[str, str], enable_mcp: bool) -> bool:
    """True iff ``--enable-mcp`` is set AND M2M client creds are present in the environment.

    M2M requires an OAuth service-principal client id + secret (the standard Databricks SDK
    ``DATABRICKS_CLIENT_ID`` / ``DATABRICKS_CLIENT_SECRET`` pair).
    """
    if not enable_mcp:
        return False
    return bool(env.get("DATABRICKS_CLIENT_ID")) and bool(env.get("DATABRICKS_CLIENT_SECRET"))


def manual_step_message(app_url: str) -> str:
    """The printed manual instruction when the MCP leg cannot run automatically."""
    return (
        "MCP leg = manual: M2M creds not available (need --enable-mcp + DATABRICKS_CLIENT_ID / "
        "DATABRICKS_CLIENT_SECRET, and an account-admin OAuth app connection for the app). "
        f"Verify by hand: connect an MCP client to {app_url}/mcp and call "
        'search_code(query="<a term you indexed>"); expect file-grouped matches with line '
        "numbers."
    )


# ------------------------------------------------------------------------------ live legs
#
# Heavy imports (httpx / mcp / sqlalchemy) are lazy so the pure predicates above stay
# import-light and unit-testable without a live deployment.


def _http_get_json(url: str) -> tuple[int, dict[str, Any]]:
    """GET ``url`` and return ``(status_code, json_body)``; a non-JSON body yields ``{}``."""
    import httpx

    resp = httpx.get(url, timeout=15.0)
    try:
        body = resp.json()
    except Exception:
        body = {}
    return resp.status_code, body


def _check_health(app_url: str) -> Result:
    status, body = _http_get_json(f"{app_url}/health")
    if status == 200 and health_ok(body):
        return Result(True, "GET /health = 200 status:ok")
    return Result(False, f"GET /health = {status} body={body!r}")


def _check_ready(app_url: str) -> Result:
    status, _ = _http_get_json(f"{app_url}/ready")
    if ready_ok(status):
        return Result(True, "GET /ready = 200 (app SP SELECT grant landed)")
    return Result(False, f"GET /ready = {status} (grant oracle: app SP cannot SELECT, or DB down)")


def _check_db(expect_indexed: bool) -> list[Result]:
    """Direct-SQL legs as the developer/owner: connectivity always; corpus under the flag.

    The corpus query assumes the default schema (``repos`` on the connection's ``search_path``),
    which is what ``make deploy`` uses (migrate resolves ``current_schema()`` when ``PGSCHEMA`` is
    unset). ``/ready`` — not this leg — is the authoritative grant oracle; this is owner-side
    connectivity plus an optional corpus check.
    """
    from sqlalchemy import text

    from app.db.client import create_db_engine

    results: list[Result] = []
    engine = create_db_engine()
    try:
        with engine.connect() as conn:
            scalar = conn.execute(text("SELECT 1")).scalar()
            results.append(assert_connectivity(scalar))
            if expect_indexed:
                count = conn.execute(text("SELECT count(*) FROM repos")).scalar()
                results.append(assert_corpus_nonempty(int(count or 0)))
    finally:
        engine.dispose()
    return results


def _check_mcp(app_url: str, query: str) -> Result:
    """Live MCP leg: authenticate M2M, call ``search_code``, validate the envelope."""
    import asyncio
    import json

    # This leg sends a real Databricks OAuth bearer to app_url; refuse a non-TLS URL so the
    # token can never be emitted in cleartext (or to an attacker-supplied http:// host).
    if not app_url.startswith("https://"):
        return Result(
            False,
            f"MCP leg refused: --app-url must be https:// to carry the OAuth bearer "
            f"(got {app_url!r})",
        )

    async def _run() -> Result:
        from databricks.sdk import WorkspaceClient
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = WorkspaceClient().config.authenticate()  # {"Authorization": "Bearer ..."}
        async with streamablehttp_client(f"{app_url}/mcp", headers=headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                res = await session.call_tool("search_code", {"query": query})
                text_parts = [c.text for c in res.content if getattr(c, "type", None) == "text"]
                payload = json.loads(text_parts[0]) if text_parts else {}
                return validate_search_payload(payload)

    return asyncio.run(_run())


# ------------------------------------------------------------------------------------ main


def _print_leg(name: str, status: str, detail: str) -> None:
    print(f"[{status:>7}] {name}: {detail}")


def main(argv: list[str] | None = None) -> int:
    import os

    parser = argparse.ArgumentParser(description="Post-deploy smoke test for the MCP app.")
    parser.add_argument("--app-url", required=True, help="Base URL of the deployed app.")
    parser.add_argument(
        "--expect-indexed",
        action="store_true",
        help="Also assert the corpus is non-empty (SELECT count(*) FROM repos >= 1).",
    )
    parser.add_argument(
        "--enable-mcp",
        action="store_true",
        help="Attempt the live MCP search_code leg (requires M2M client creds).",
    )
    parser.add_argument(
        "--query",
        default="the",
        help="Query for the MCP search_code leg (default: a common term).",
    )
    args = parser.parse_args(argv)

    app_url = args.app_url.rstrip("/")
    failed = False

    def _guard(name: str, fn: Any) -> Result:
        """Turn a live leg's exception into a gating FAIL line, not a raw traceback."""
        try:
            return fn()
        except Exception as exc:  # unreachable app / engine build failure / etc.
            return Result(False, f"{name}: raised {type(exc).__name__}: {exc}")

    # /health and /ready — always gating.
    health = _guard("health", lambda: _check_health(app_url))
    ready = _guard("ready", lambda: _check_ready(app_url))
    for name, result in (("health", health), ("ready", ready)):
        _print_leg(name, "PASS" if result.ok else "FAIL", result.detail)
        failed = failed or not result.ok

    # Direct-SQL: connectivity always; corpus only under --expect-indexed. Both gating. A raised
    # engine/connection error becomes a single gating FAIL line rather than crashing the run.
    try:
        db_results = _check_db(args.expect_indexed)
    except Exception as exc:
        db_results = [Result(False, f"raised {type(exc).__name__}: {exc}")]
    for result in db_results:
        _print_leg("direct-sql", "PASS" if result.ok else "FAIL", result.detail)
        failed = failed or not result.ok

    # MCP: automated only when M2M is available; otherwise a documented, non-gating manual step
    # UNLESS --enable-mcp was requested (then a failure is gating).
    if m2m_available(os.environ, args.enable_mcp):
        result = _check_mcp(app_url, args.query)
        _print_leg("mcp", "PASS" if result.ok else "FAIL", result.detail)
        failed = failed or not result.ok
    elif args.enable_mcp:
        _print_leg("mcp", "FAIL", "--enable-mcp set but M2M creds are missing")
        failed = True
    else:
        _print_leg("mcp", "SKIPPED", manual_step_message(app_url))

    print("smoke: FAIL" if failed else "smoke: PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
