"""PR-level selection: keep the few findings a maintainer would actually post.

Generation and per-file verification judge each finding in isolation, so a PR
ends up with every defensible issue (~9 per PR on the benchmark) while human
reviewers leave ~3-4. Every extra comment costs reader trust and benchmark
precision, so one final call sees all surviving findings together, merges
cross-file duplicates, and keeps a handful in priority order.

Candidates are shown in diff order without their severity labels: generation
marks most findings critical, and a selector shown those labels ranks by them
instead of by what the diff demonstrates.
"""
from __future__ import annotations

import logging

from django.conf import settings

from .budget import Budget, BudgetExhausted
from .diffs import fence
from .findings import severity_rank, sort_by_location
from .llm import Completer, structured_call
from .reviewer import INPUT_RULES

LOGGER = logging.getLogger("reviews.selection")

SELECT_SCHEMA: dict = {
    "type": "object",
    "required": ["selected"],
    "properties": {
        "selected": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["index"],
                "properties": {
                    "index": {"type": "integer"},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}

SELECT_SYSTEM_PROMPT = "You are a senior maintainer triaging automated review comments. Reply only JSON."

SELECT_PROMPT = """An automated reviewer produced the candidate findings below for one pull request. You decide which ones get posted. Post at most {top_k}; fewer is better when the rest are weak. Every posted comment that is wrong, hedged, duplicated, or not worth a maintainer's time costs the reviewer its credibility.

Severity labels are deliberately omitted; judge each candidate from the diff. Rank candidates by how certainly and how badly the changed code misbehaves:
1. Definite failures on a normal path: crash/exception, wrong result, broken contract with a base class or caller, security or authorization hole, data loss.
2. Definite failures confined to an edge case, error path, or cleanup path.
3. Concrete but minor defects (inconsistent metric tag, misleading docstring or message).

Do NOT post:
- DUPLICATES: two candidates with the same root cause or the same fix (even in different files or lines). Post only the clearest one.
- HEDGED: "may", "might", "could", or a conditional failure ("raises TypeError if X does not accept Y") where the diff does not show that the condition holds.
- Advice without a demonstrated defect: missing tests, timeouts, logging, validation "for robustness", refactors, style.
- Findings that misread the diff or describe unchanged code.

{input_rules}

PR title: {pr_title}

Diff under review:
{diff}

Candidates (0-indexed):
{candidates}

Respond with ONLY JSON listing the candidates to post, most important first:
{{"selected":[{{"index":0,"reason":"brief"}}]}}"""


def _conf(name, default):
    return getattr(settings, name, default)


def _confidence(finding: dict) -> float:
    try:
        return float(finding.get("confidence", 1.0))
    except (TypeError, ValueError):
        return 1.0


def post_limit(candidates: int, top_k: int) -> int:
    """How many findings a PR may post: about a third of what survived verify.

    Large PRs legitimately carry more issues than small ones, but verified
    candidates are still mostly noise, so the cap grows slowly and stops at
    ``top_k``.
    """
    return min(top_k, max(3, round(candidates / 3)))


def fallback_top_k(findings: list[dict], top_k: int) -> list[dict]:
    """Deterministic ranking used when the selection call degrades."""
    ranked = sorted(findings, key=lambda f: (severity_rank(f), -_confidence(f)))
    return ranked[:top_k]


def select_findings(
    completer: Completer,
    budget: Budget,
    *,
    pr_title: str,
    diff_text: str,
    findings: list[dict],
    top_k: int,
) -> list[dict]:
    """Return at most ``post_limit(len(findings), top_k)`` findings, most important first."""
    if top_k <= 0 or len(findings) <= 1:
        return findings[:top_k] if top_k > 0 else findings

    findings = sort_by_location(findings)
    limit = post_limit(len(findings), top_k)
    listing = "\n".join(
        f"[{i}] ({f.get('category')}) {f.get('path')}:{f.get('line')} — {f.get('text')}"
        for i, f in enumerate(findings)
    )
    try:
        result = structured_call(
            completer, budget, session="select",
            system_prompt=SELECT_SYSTEM_PROMPT,
            prompt=SELECT_PROMPT.format(
                top_k=limit,
                input_rules=INPUT_RULES,
                pr_title=pr_title,
                diff=fence(diff_text, int(_conf("PRCHECK_REVIEW_DIFF_LIMIT", 120_000))),
                candidates=listing,
            ),
            schema=SELECT_SCHEMA, stage="select",
        )
    except BudgetExhausted:
        result = None
    if result is None:
        return fallback_top_k(findings, limit)

    selected: list[dict] = []
    seen: set[int] = set()
    for item in result.get("selected", []):
        index = item.get("index")
        # Out-of-range and repeated indices are selector mistakes, not findings.
        if type(index) is not int or not 0 <= index < len(findings) or index in seen:
            continue
        seen.add(index)
        selected.append(findings[index])
        if len(selected) >= limit:
            break
    LOGGER.info("reviews_selected kept=%d of=%d", len(selected), len(findings))
    return selected
