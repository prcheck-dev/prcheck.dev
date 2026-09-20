"""Verdict computation: a pure function in harness code.

The model writes findings; it does not choose the verdict. The verdict is
derived from the finding severities and run conditions. Ordering:
block < request-changes < approve-with-conditions < approve; a cap can only
move the verdict down, never up.
"""
from __future__ import annotations

from collections import Counter

APPROVE = "approve"
APPROVE_COND = "approve-with-conditions"
REQUEST_CHANGES = "request-changes"
BLOCK = "block"

_ORDER = [BLOCK, REQUEST_CHANGES, APPROVE_COND, APPROVE]


def _cap(verdict: str, cap: str) -> str:
    return verdict if _ORDER.index(verdict) <= _ORDER.index(cap) else cap


def compute_verdict(findings: list[dict], *, degraded: bool = False) -> tuple[str, list[str]]:
    """Return ``(verdict, conditions)`` for a set of reviewer findings."""
    counts = Counter(str(f.get("severity") or "").lower() for f in findings)
    conditions: list[str] = []

    if counts.get("critical"):
        return BLOCK, conditions
    if counts.get("high"):
        return REQUEST_CHANGES, conditions

    verdict = APPROVE
    if counts.get("medium") or counts.get("low"):
        n = counts.get("medium", 0) + counts.get("low", 0)
        conditions.append(f"{n} non-blocking finding(s) to consider")
        verdict = _cap(verdict, APPROVE_COND)

    if degraded:
        conditions.append("review incomplete (model unavailable / degraded run)")
        verdict = _cap(verdict, APPROVE_COND)

    return verdict, conditions
