"""Canonical content hash for content-deduped multi-branch storage.

The single source of truth for ``files.content_sha``. The ``0003`` migration's
backfill (``encode(digest(coalesce(content,''),'sha256'),'hex')``) and
``indexer/store.py``'s per-file upsert must produce byte-identical output to
this function forever, or dedup across branches silently breaks (a repeat
index of unchanged content would mint a new ``content_sha`` and orphan the old
row). ``tests/integration/test_content_sha_parity.py`` is the hard gate that
proves it.
"""

from __future__ import annotations

import hashlib


def content_sha(content: str | None) -> str:
    """SHA-256 hex digest of ``content``, treating ``None`` as the empty string."""
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()
