"""High-recall deep review: per-file issue-list generation, then verification.

Motivation (grounded in recent code-review research):
  * A single precision-tuned whole-diff pass under-reports on large PRs and
    misses function-level defects. Reviewing each changed file on its own, with
    the enclosing file as context, restores coverage.
  * "Issue-list" prompting — explore ALL plausible issues rather than only the
    top few — measurably improves recall; the usual "emit at most N / omit if
    unsure" instructions actively suppress real findings.
  * Over-generation is made safe by a second VERIFY pass that drops anything not
    concretely supported by the diff (generate-then-verify), which is how
    production reviewers keep precision without sacrificing recall.

The generate checklist is derived from the concrete defect classes our own
benchmark run missed (null-deref, KeyError, falsy-value bugs, order
assumptions, missing interface methods, analytics-before-guard, …).
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings

from .budget import Budget
from .llm import Completer, structured_call
from .schema import REVIEWER_SCHEMA, VERIFY_SCHEMA

_DIFF_FILE = re.compile(r"^diff --git a/(.*?) b/(.*?)$", re.M)
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

GENERATE_PROMPT = """You are prcheck's deep code reviewer, reviewing ONE changed file of a pull request.

Your goal here is maximum RECALL. Enumerate EVERY plausible concrete issue introduced by the changed (+) lines in this file. Do NOT limit the number of findings, and do NOT skip an issue because you are unsure — instead give it a lower `confidence`. A separate verification stage removes weak findings afterwards, so over-reporting here is expected and correct.

Walk each changed function/hunk and systematically check this checklist against the changed lines:
- Null/None/undefined dereference; Optional/nil unwrapped without a guard.
- KeyError / missing dict key; indexing a result that can be empty (`x[0]`).
- Type errors: wrong type passed; numeric ops (floor/ceil/math) on non-numbers or datetimes.
- Falsy-value bugs: `0`, `0.0`, `""`, `False` treated as absent via truthiness or `.get(...)`.
- Off-by-one / boundary / negative offset or slice.
- Order assumptions: zipping keys with `dict.values()`, assuming query/result ordering.
- Interface/API: an abstract method left unimplemented; a changed signature not reflected in callers shown here; a return type that no longer matches the docstring/base class.
- Concurrency/resources: unawaited async, races, partial cleanup, swallowed errors, leaks, processes/handles not terminated.
- Security: string-interpolated SQL/commands, auth/authz bypass, case-insensitive bypass of a case-sensitive check, missing/loosened validation.
- Control flow: analytics/logging/metrics emitted before an early-return guard; inconsistent metric labels across branches.
- Documentation: a docstring/comment/error message that contradicts the implementation.
- Data: hardcoded/empty data passed where real data is expected; migrations that skip normalization.

Use the full file content for context, but only report issues INTRODUCED by this diff. One issue per finding, each with the exact path and changed line number.

Respond with ONLY JSON:
{"findings":[{"text":"specific issue and concrete failing path","path":"changed/file.ext","line":123,"severity":"critical|high|medium|low","category":"bug|security|concurrency|data|api|perf|test_gap|doc_defect|style","confidence":0.0-1.0}]}"""

VERIFY_PROMPT = """You are prcheck's verification stage. Decide which candidate findings are REAL, concrete issues introduced by this diff and worth a senior engineer's attention.

REJECT a candidate if ANY of these hold:
- ASSUMPTION: it assumes the existence or behavior of code, config, callers, imports, or dependencies that are NOT present in the provided diff/file (e.g., "X may not accept this kwarg", "a .gitmodules entry is missing", "the caller probably does Y"). If you cannot confirm it from what is shown, reject it.
- WRONG: it misreads the changed logic, or its failing path does not actually hold given the code shown.
- NON-ACTIONABLE: it does not imply a concrete code change — vague advice, restating the code, or "consider ..." with no demonstrated defect.
- DUPLICATE: it describes the same underlying fix as another (usually higher-ranked) candidate. Keep only one.
- NOISE: trivial/obvious with no real impact.

KEEP genuine bugs, security/auth issues, data-loss risks, and concrete correctness/interface defects — even minor ones — when the diff itself demonstrates the failing path.

Diff under review:
{diff}

Candidates (0-indexed):
{candidates}

Respond with ONLY JSON — a verdict for EVERY index:
{{"results":[{{"index":0,"keep":true,"reason":"brief"}}]}}"""


def _conf(name, default):
    return getattr(settings, name, default)


def _diff_blocks(diff_text):
    matches = list(_DIFF_FILE.finditer(diff_text))
    if not matches:
        return [(None, diff_text)]
    out = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(diff_text)
        out.append((m.group(2), diff_text[m.start():end]))
    return out


def _fence(text, limit):
    if len(text) > limit:
        half = limit // 2
        text = text[:half] + "\n[... truncated ...]\n" + text[-half:]
    return f"<untrusted>\n{text}\n</untrusted>"


def run_deep_review(
    completer: Completer,
    budget: Budget,
    *,
    pr_title: str,
    pr_url: str,
    changed_files: list[dict],
    diff_text: str,
    file_contents: dict[str, str] | None = None,
) -> dict | None:
    """Generate per-file issue lists (high recall), then verify (precision)."""
    file_contents = file_contents or {}
    blocks = [(p, t) for p, t in _diff_blocks(diff_text) if p]
    if not blocks:
        blocks = [(None, diff_text)]

    # Bound the number of per-file generate calls on very large PRs (e.g. a
    # 100+ file refactor): review the most-changed files first so we stay within
    # budget instead of degrading to zero findings.
    max_files = int(_conf("PRCHECK_DEEP_MAX_FILES", 40))
    if len(blocks) > max_files:
        blocks = sorted(blocks, key=lambda b: len(b[1]), reverse=True)[:max_files]

    ctx_limit = int(_conf("PRCHECK_DEEP_FILE_BYTES", 60_000))
    diff_limit = int(_conf("PRCHECK_REVIEW_DIFF_LIMIT", 120_000))
    max_workers = max(1, int(_conf("PRCHECK_REVIEW_MAX_WORKERS", 4)))

    def _generate(block):
        path, block_diff = block
        content = file_contents.get(path or "", "")
        # File context is the main recall lever (measured), so always include it,
        # truncated to a generous cap rather than omitted — the reasoning-budget
        # and turn-budget fixes make large context safe now.
        prompt = (
            f"PR title: {pr_title}\nFile: {path}\n\n"
            f"Diff for this file:\n{_fence(block_diff, diff_limit)}\n\n"
            + (f"Full current file (context only):\n{_fence(content, ctx_limit)}\n" if content else "")
        )
        return structured_call(
            completer, budget, session="deep-generate",
            system_prompt=GENERATE_PROMPT, prompt=prompt,
            schema=REVIEWER_SCHEMA, stage="review",
        )

    results = []
    if len(blocks) == 1:
        results = [_generate(blocks[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(blocks))) as pool:
            results = list(pool.map(_generate, blocks))

    candidates: list[dict] = []
    for r in results:
        for f in (r or {}).get("findings", []):
            if isinstance(f, dict):
                candidates.append(f)
    if not candidates:
        return {"findings": [], "_deep": True} if any(r is not None for r in results) else None

    candidates = _dedupe(candidates)
    kept = _verify(completer, budget, diff_text, candidates) if _conf("PRCHECK_DEEP_VERIFY", True) else candidates

    limit = int(_conf("PRCHECK_REVIEW_MAX_FINDINGS", 40))
    kept.sort(key=lambda f: _SEVERITY_RANK.get(str(f.get("severity", "")).lower(), 4))
    return {"findings": kept[:limit], "_deep": True, "_generated": len(candidates)}


def _verify(completer, budget, diff_text, candidates):
    listing = "\n".join(
        f"[{i}] ({c.get('severity')}/{c.get('category')}) {c.get('path')}:{c.get('line')} — {c.get('text')}"
        for i, c in enumerate(candidates)
    )
    result = structured_call(
        completer, budget, session="deep-verify",
        system_prompt="You are a precise code-review verifier. Reply only JSON.",
        prompt=VERIFY_PROMPT.format(diff=_fence(diff_text, int(_conf("PRCHECK_REVIEW_DIFF_LIMIT", 120_000))),
                                    candidates=listing),
        schema=VERIFY_SCHEMA, stage="adversarial",
    )
    if result is None:  # verify degraded -> keep candidates rather than lose recall
        return candidates
    keep_idx = {r["index"] for r in result.get("results", []) if r.get("keep")}
    return [c for i, c in enumerate(candidates) if i in keep_idx]


def _tokens(text):
    return set(re.findall(r"[a-z0-9_]{3,}", str(text).casefold()))


def _dedupe(candidates):
    """Drop exact and near-duplicate findings.

    Two findings are near-duplicates when they sit on the same file within a few
    lines and their token sets overlap heavily (Jaccard > 0.6) — this catches the
    same defect reported with different wording.
    """
    out = []
    for c in candidates:
        ct = _tokens(c.get("text"))
        cpath, cline = str(c.get("path")), c.get("line")
        dup = False
        for k in out:
            if str(k.get("path")) != cpath:
                continue
            close = isinstance(cline, int) and isinstance(k.get("line"), int) and abs(cline - k["line"]) <= 3
            kt = _tokens(k.get("text"))
            union = ct | kt
            jac = len(ct & kt) / len(union) if union else 0
            if (cline == k.get("line") and jac > 0.5) or (close and jac > 0.6):
                dup = True
                break
        if not dup:
            out.append(c)
    return out
