"""Runtime settings for the MCP server (pydantic-settings).

Config lives in one place so the tool layer never reads ``os.environ`` directly.
Two naming conventions co-exist on purpose:

* Tunables (timeouts, caps, limits) use the ``CODE_SEARCH_`` env prefix so they are
  namespaced and obviously ours.
* ``lakebase_endpoint`` reads the **unprefixed** ``LAKEBASE_ENDPOINT`` via a
  ``validation_alias`` because that is exactly the variable ``app/db/client.py`` already
  understands (and the same value the indexing job / DABs bundle export). Aliasing it here
  keeps a single deploy-time contract for the endpoint identifier.

``get_settings()`` is a process-cached accessor: settings are read once from the
environment and reused, matching the process-scoped engine singleton in ``app/main.py``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Server configuration, read from the environment once per process."""

    model_config = SettingsConfigDict(env_prefix="CODE_SEARCH_", extra="ignore")

    # Lakebase endpoint identifier, threaded into create_db_engine(endpoint=...). Reads the
    # unprefixed LAKEBASE_ENDPOINT (client.py's own contract); None -> client.py falls back
    # to its own env lookup / raises if truly absent.
    lakebase_endpoint: str | None = Field(default=None, validation_alias="LAKEBASE_ENDPOINT")

    # Per-request DB-time bound, threaded into grep_search and the raw list_repos/get_file
    # SELECTs and the /ready probe. Bounds how long a single blocking worker can pin the pool.
    statement_timeout_ms: int = 5000

    # Aggregate content bytes grep pulls/scans per request (memory bound).
    max_content_bytes: int = 8 * 1024 * 1024

    # Default and hard-cap for search_code(limit): <=0 -> row_limit; > max -> max_row_limit.
    row_limit: int = 200
    max_row_limit: int = 1000

    # Declared but unwired this issue; a later semantic issue owns its code path.
    semantic_enabled: bool = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-cached :class:`Settings` (read from the environment once)."""
    return Settings()
