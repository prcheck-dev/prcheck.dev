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
from concurrent.futures import ThreadPoolExecutor

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

SELECT_PROMPT = """An automated reviewer produced the candidate findings below for one pull request. You decide which ones get posted. {quota} Every posted comment that is wrong, hedged, duplicated, or not worth a maintainer's time costs the reviewer its credibility.

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


def _label(finding: dict) -> str:
    # The generator's own confidence ranks findings about as well as the
    # selector does (AUC 0.73 each on the benchmark), so it can be shown.
    if _conf("PRCHECK_SELECT_SHOW_CONFIDENCE", False) and finding.get("confidence") is not None:
        try:
            return f"{finding.get('category')}, reviewer confidence {float(finding['confidence']):.2f}"
        except (TypeError, ValueError):
            pass
    return str(finding.get("category"))


def _quota(limit: int) -> str:
    # A reasoning selector reads "fewer is better" literally and posts ~2 per
    # PR; the fill wording keeps its precision while posting up to the limit.
    if _conf("PRCHECK_SELECT_FILL", False):
        return (f"Post {limit} findings, most important first; post fewer only when fewer than "
                f"{limit} candidates are real defects in the changed code.")
    return f"Post at most {limit}; fewer is better when the rest are weak."


def post_limit(candidates: int, top_k: int) -> int:
    """How many findings a PR may post: a share of what survived verify.

    Large PRs legitimately carry more issues than small ones, but verified
    candidates are still mostly noise, so the cap grows slowly and stops at
    ``top_k``.
    """
    minimum = int(_conf("PRCHECK_REVIEW_MIN_POSTS", 3))
    share = float(_conf("PRCHECK_REVIEW_POST_SHARE", 1 / 3))
    return min(top_k, max(minimum, round(candidates * share)))


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
    """Return the findings to post, most important first.

    With ``PRCHECK_SELECT_VOTES`` > 1 the selector runs that many times in
    parallel and a finding is posted when a majority of runs picked it: single
    runs disagree often enough that the agreed picks are more reliable
    (benchmark F1 +0.006 and +0.012 on two runs' findings).
    """
    if top_k <= 0 or len(findings) <= 1:
        return findings[:top_k] if top_k > 0 else findings
    findings = sort_by_location(findings)
    votes = max(1, int(_conf("PRCHECK_SELECT_VOTES", 1)))
    if votes == 1:
        return _select_once(completer, budget, pr_title=pr_title, diff_text=diff_text,
                            findings=findings, top_k=top_k)
    with ThreadPoolExecutor(max_workers=votes) as pool:
        draws = list(pool.map(
            lambda _: _select_once(completer, budget, pr_title=pr_title, diff_text=diff_text,
                                   findings=findings, top_k=top_k),
            range(votes),
        ))
    counts: dict[int, int] = {}
    first_rank: dict[int, tuple[int, int]] = {}
    for d, picks in enumerate(draws):
        for rank, finding in enumerate(picks):
            key = id(finding)
            counts[key] = counts.get(key, 0) + 1
            first_rank.setdefault(key, (d, rank))
    majority = votes // 2 + 1
    by_id = {id(f): f for f in findings}
    agreed = sorted((k for k, n in counts.items() if n >= majority),
                    key=lambda k: (-counts[k], first_rank[k]))
    LOGGER.info("reviews_selected_by_vote kept=%d votes=%d", len(agreed), votes)
    return [by_id[k] for k in agreed]


def _select_once(completer, budget, *, pr_title, diff_text, findings, top_k) -> list[dict]:
    """One selector call over location-sorted ``findings``; returns elements of it."""
    limit = post_limit(len(findings), top_k)
    listing = "\n".join(
        f"[{i}] ({_label(f)}) {f.get('path')}:{f.get('line')} — {f.get('text')}"
        for i, f in enumerate(findings)
    )
    try:
        result = structured_call(
            completer, budget, session="select",
            system_prompt=SELECT_SYSTEM_PROMPT,
            prompt=SELECT_PROMPT.format(
                quota=_quota(limit),
                input_rules=INPUT_RULES,
                pr_title=pr_title,
                diff=fence(diff_text, int(_conf("PRCHECK_REVIEW_DIFF_LIMIT", 120_000))),
                candidates=listing,
            ),
            schema=SELECT_SCHEMA, stage="select",
            reasoning_effort=str(_conf("PRCHECK_SELECT_REASONING_EFFORT", "") or "") or None,
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


def _confidence_of(finding: dict) -> float | None:
    try:
        return float(finding["confidence"]) if finding.get("confidence") is not None else None
    except (TypeError, ValueError):
        return None


def adjust_by_confidence(chosen: list[dict], pool: list[dict]) -> list[dict]:
    """Combine the selector's picks with the generator's own confidence.

    Each signal ranks findings about as well as the other (AUC ~0.73 on the
    benchmark) and they disagree often enough to combine: picks the generator
    itself doubted are dropped, and findings it was near-certain about are
    added when the selector left them out, up to a small total.
    """
    floor = float(_conf("PRCHECK_SELECT_MIN_CONFIDENCE", 0.7))
    certain = float(_conf("PRCHECK_POST_CERTAIN_CONFIDENCE", 0.9))
    fill_to = int(_conf("PRCHECK_POST_CERTAIN_FILL_TO", 4))
    kept = [f for f in chosen if (_confidence_of(f) is None or _confidence_of(f) >= floor)]
    extras = [f for f in pool
              if f not in kept and (_confidence_of(f) or 0) >= certain and f.get("source") != "adversary"]
    return (kept + extras)[:max(fill_to, len(kept))]
