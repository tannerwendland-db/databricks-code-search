"""Unit tests for indexer.branches.resolve_branches: glob match, dedup, cap."""

from __future__ import annotations

import logging

import pytest

from indexer.branches import SOFT_BRANCH_CAP, resolve_branches


@pytest.mark.unit
def test_empty_globs_is_default_branch_only() -> None:
    """The documented default: no branches: configured -> just the default."""
    got = resolve_branches("main", ["main", "dev", "release/1.0"], [], repo="acme/widgets")
    assert got == ["main"]


@pytest.mark.unit
def test_empty_globs_ignores_all_branches_entirely() -> None:
    """Even a nonempty all_branches list is irrelevant when globs is empty."""
    got = resolve_branches("main", [], [], repo="acme/widgets")
    assert got == ["main"]


@pytest.mark.unit
def test_default_branch_always_included_even_if_unmatched() -> None:
    got = resolve_branches("main", ["main", "dev"], ["release/*"], repo="acme/widgets")
    assert "main" in got


@pytest.mark.unit
def test_glob_matches_via_fnmatchcase() -> None:
    got = resolve_branches(
        "main",
        ["main", "release/1.0", "release/2.0", "dev"],
        ["release/*"],
        repo="acme/widgets",
    )
    assert set(got) == {"main", "release/1.0", "release/2.0"}


@pytest.mark.unit
def test_fnmatchcase_is_case_sensitive() -> None:
    got = resolve_branches("main", ["Release/1.0"], ["release/*"], repo="acme/widgets")
    assert got == ["main"]  # "Release/1.0" does not match the lowercase glob


@pytest.mark.unit
def test_multiple_globs_are_unioned() -> None:
    got = resolve_branches(
        "main",
        ["main", "staging", "hotfix/x", "dev"],
        ["staging", "hotfix/*"],
        repo="acme/widgets",
    )
    assert set(got) == {"main", "staging", "hotfix/x"}


@pytest.mark.unit
def test_default_branch_matching_a_glob_is_not_duplicated() -> None:
    got = resolve_branches("main", ["main", "dev"], ["m*"], repo="acme/widgets")
    assert got == ["main"]


@pytest.mark.unit
def test_ordering_is_default_first_then_alphabetical() -> None:
    got = resolve_branches("main", ["main", "zeta", "alpha", "beta"], ["*"], repo="acme/widgets")
    assert got == ["main", "alpha", "beta", "zeta"]


@pytest.mark.unit
def test_cap_truncates_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    all_branches = ["main"] + [f"b{i:02d}" for i in range(30)]
    with caplog.at_level(logging.WARNING, logger="indexer.branches"):
        got = resolve_branches("main", all_branches, ["*"], repo="acme/widgets", cap=5)

    assert len(got) == 5
    assert got[0] == "main"
    # default-first then alphabetical, truncated at 5: main, b00, b01, b02, b03.
    assert got == ["main", "b00", "b01", "b02", "b03"]

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "acme/widgets" in message
    assert "31 branches" in message
    assert "dropping 26" in message


@pytest.mark.unit
def test_no_warning_when_under_the_cap(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="indexer.branches"):
        resolve_branches("main", ["main", "dev"], ["*"], repo="acme/widgets", cap=5)
    assert caplog.records == []


@pytest.mark.unit
def test_default_cap_is_the_module_constant() -> None:
    all_branches = [f"b{i:02d}" for i in range(SOFT_BRANCH_CAP + 5)]
    got = resolve_branches("main", all_branches, ["*"], repo="acme/widgets")
    assert len(got) == SOFT_BRANCH_CAP
