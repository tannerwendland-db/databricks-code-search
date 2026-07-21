"""Resolve a config connection's branch globs into a concrete per-repo branch list.

The default branch is always included, regardless of whether it matches a glob
-- a repo can never resolve to zero branches. Empty ``globs`` means
default-branch-only (``indexer.repo_config.GitHubConnection.branches``'
documented default), resolved WITHOUT consulting ``all_branches`` at all: the
caller (``indexer.job``) skips the GitHub branches API call entirely in that
common case.
"""

from __future__ import annotations

import logging
from fnmatch import fnmatchcase

logger = logging.getLogger("indexer.branches")

# A runaway config (a typo'd broad glob like `*`) must not fan a single repo out
# into an unbounded number of per-branch indexing runs. Overridable only by
# narrowing the globs in config.yaml -- there is no override flag, mirroring
# indexer.resolve.MAX_REPOS' philosophy of a config-level fix over a knob.
SOFT_BRANCH_CAP = 20


def resolve_branches(
    default_branch: str,
    all_branches: list[str],
    globs: list[str],
    *,
    repo: str,
    cap: int = SOFT_BRANCH_CAP,
) -> list[str]:
    """Resolve ``globs`` against ``all_branches`` for one repo.

    ``default_branch`` is always first in the result. The rest are every branch
    in ``all_branches`` matching any glob (``fnmatchcase`` -- a plain glob against
    the exact branch name, identically on every platform), deduped and sorted
    alphabetically. If the result exceeds ``cap``, it is truncated to ``cap``
    (default-first, alphabetical) with a loud warning naming ``repo`` and the
    dropped count -- a truncation, not a failure, so indexing still proceeds.
    """
    if not globs:
        return [default_branch]

    matched = {b for b in all_branches if any(fnmatchcase(b, g) for g in globs)}
    matched.add(default_branch)

    rest = sorted(b for b in matched if b != default_branch)
    ordered = [default_branch, *rest]

    if len(ordered) > cap:
        dropped = ordered[cap:]
        logger.warning(
            "%s: resolved %d branches, above the soft cap of %d; dropping %d "
            "(keeping default-first, alphabetical): %s",
            repo,
            len(ordered),
            cap,
            len(dropped),
            ", ".join(dropped),
        )
        ordered = ordered[:cap]

    return ordered
