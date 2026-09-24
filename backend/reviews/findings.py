"""Harness-side finding hygiene: grounding, near-duplicate merging, ordering.

The model proposes findings; code decides which ones are allowed to exist. A
finding survives only if it points at a line inside the diff of a changed file,
which removes the largest source of noise (hallucinated or pre-existing-code
findings) before anything is stored or published.
"""
from __future__ import annotations

import logging
import re

from .schema import SEVERITIES

LOGGER = logging.getLogger("reviews.findings")

SEVERITY_RANK = {severity: rank for rank, severity in enumerate(SEVERITIES)}


def severity_rank(finding: dict) -> int:
    return SEVERITY_RANK.get(str(finding.get("severity") or "").lower(), len(SEVERITY_RANK))


def sort_findings(findings: list[dict]) -> list[dict]:
    return sorted(findings, key=lambda f: (
        severity_rank(f), str(f.get("path") or ""), _int_or_zero(f.get("line")),
    ))


def _int_or_zero(value) -> int:
    return value if type(value) is int else 0


def _normalise_path(path) -> str:
    path = str(path or "").strip().strip("`")
    for prefix in ("a/", "b/", "./"):
        if path.startswith(prefix):
            path = path[len(prefix):]
    return path


def _resolve_path(path: str, known: dict) -> str | None:
    if path in known:
        return path
    # Models sometimes drop a leading directory; accept only an unambiguous match.
    matches = [candidate for candidate in known if candidate.endswith("/" + path)]
    return matches[0] if len(matches) == 1 else None


def ground_findings(findings: list[dict], lines_by_path: dict[str, dict[str, set[int]]],
                    *, window: int = 3) -> list[dict]:
    """Keep findings anchored in the diff, snapping near-misses to a changed line.

    A line on a commentable (added or context) line is kept as is. A line
    within ``window`` of an added line is snapped to the nearest one, which
    absorbs small counting errors. Anything else is dropped.
    """
    grounded: list[dict] = []
    for finding in findings:
        path = _resolve_path(_normalise_path(finding.get("path")), lines_by_path)
        line = finding.get("line")
        if path is None or type(line) is not int:
            LOGGER.info("reviews_finding_dropped reason=path path=%s", finding.get("path"))
            continue
        lines = lines_by_path[path]
        if line not in lines["commentable"]:
            nearest = min(lines["added"], key=lambda n: (abs(n - line), n), default=None)
            if nearest is None or abs(nearest - line) > window:
                LOGGER.info("reviews_finding_dropped reason=line path=%s line=%s", path, line)
                continue
            line = nearest
        grounded.append({**finding, "path": path, "line": line})
    return grounded


def _tokens(text) -> set[str]:
    return set(re.findall(r"[a-z0-9_]{3,}", str(text).casefold()))


def dedupe_findings(findings: list[dict]) -> list[dict]:
    """Drop exact and near-duplicate findings, keeping the most severe copy.

    Two findings are near-duplicates when they sit on the same file within a
    few lines and their wording overlaps heavily; shards, per-file passes and
    the adversary often restate the same defect in different words.
    """
    ordered = sorted(
        findings,
        key=lambda f: (severity_rank(f), -float(f.get("confidence") or 0)),
    )
    kept: list[tuple[dict, set[str]]] = []
    for finding in ordered:
        tokens = _tokens(finding.get("text"))
        path, line = str(finding.get("path") or ""), finding.get("line")
        duplicate = False
        for other, other_tokens in kept:
            if str(other.get("path") or "") != path:
                continue
            union = tokens | other_tokens
            overlap = len(tokens & other_tokens) / len(union) if union else 1.0
            other_line = other.get("line")
            same = line == other_line
            close = type(line) is int and type(other_line) is int and abs(line - other_line) <= 3
            if (same and overlap > 0.5) or (close and overlap > 0.6):
                duplicate = True
                break
        if not duplicate:
            kept.append((finding, tokens))
    return [finding for finding, _ in kept]
