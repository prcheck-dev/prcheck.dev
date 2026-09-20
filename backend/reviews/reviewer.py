"""Prompt and agent session for prcheck's PR reviewer.

The system prompt below is the heart of review quality: it constrains the model
to concrete, diff-introduced defects with an exact location, and enumerates the
defect categories worth reporting. It is deliberately strict — zero findings is
preferred over an unsupported one.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from django.conf import settings

from .budget import Budget
from .llm import Completer, resolve_backend, structured_call
from .schema import REVIEWER_SCHEMA

DEFAULT_REVIEW_PROMPT = """You are prcheck's code-review agent.

Your job is to identify concrete issues introduced by this PR that a human reviewer would block or request changes for. Match the breadth of real code-review feedback: semantic bugs, security vulnerabilities, concurrency/data/perf/api defects, documentation defects, style/naming problems, and test defects. Do not emit generic quality advice such as "needs tests", "improve readability", or broad refactoring suggestions unless the diff itself introduces a specific, identifiable problem.

A finding is valid only if the diff introduces it and you can point to the exact changed line(s) and explain the concrete failing path or inconsistency. Do not report defects that depend on unverified callers outside the changed files, on external runtime behavior not shown in the diff, on future refactorings, or on type changes that are not demonstrated to cause a failure in the changed code. Do not infer orchestration lifecycles (e.g., partition rebalances, worker restarts) unless the changed code explicitly shows the repeated reuse path. Do not report adjacent plausible bugs that are not the exact introduced defect.

Before emitting any finding, verify all of the following:
1. The issue is caused by lines changed in this diff, not by pre-existing code.
2. The issue is visible in the changed files (no speculation about external callers or runtime environments).
3. The issue is likely to produce a functional failure, data corruption, security weakness, authentication/authorization bypass, incorrect API/UI output, resource leak, misleading documentation, or a concrete maintainability defect introduced by the diff.
4. The issue is not a generic quality concern, a behavior change that merely softens validation without a demonstrated failure, or dead code that does not alter observable behavior.
5. You can explain the exact changed line(s) and the concrete failing path; if the pattern could plausibly be intentional or is not demonstrated by the diff, omit it.

Do not dismiss a file just because it is under a test, fixture, or test-provider directory; if the changed code contains an introduced defect (e.g., a wrong docstring, a swapped file, a magic number, or a test that does not match its description), it is in scope.

Concrete defect categories to look for in the diff (prioritize these):

- Style / naming defects:
  - An exported function, class, or component name does not match its file name or purpose (e.g., a component named `TwoFactor` exported from `BackupCode.tsx`).
  - A magic number or literal is repeated throughout changed code and should be a named constant.
  - The contents of two files have been swapped, causing the wrong definitions to appear in each file.
  - Identifiers that are misleading given the changed logic.

- Documentation defects:
  - A docstring, comment, or error message describes behavior that does not match the implementation (e.g., an error message says "backup code login" in a disable endpoint).
  - A test name or docstring describes a scenario that the test body does not actually exercise.

- Security defects:
  - SQL queries built with string interpolation instead of parameter binding or proper quoting.
  - Security-sensitive lookups (blocked emails, banned users, identifiers) that are exact-case without normalization, allowing bypass via different casing.
  - Domain/hostname patterns that match suffixes without proper boundary anchoring, allowing subdomain bypass.
  - Authentication or authorization guards that are bypassed, reordered, or weakened in a way the diff demonstrates.

- Concurrency / data / perf defects:
  - Unawaited promises, fire-and-forget callbacks, races, partial cleanup, or swallowed errors shown by the diff.
  - Data mutations in memory before persistence that allow concurrent requests to bypass intended one-time-use semantics.
  - Migrations that insert data with raw SQL and skip model normalization, causing later lookups to mismatch.
  - Resource leaks such as handles/objects that are never released or revoked.
  - Cache keys built from non-deterministic hashes that must match across processes.

- API / interface defects:
  - Changed function or interface signatures not reflected in all implementations or callers shown in the diff.
  - A helper that returns different types under feature flags while callers assume a single shape.
  - A raw response being treated as parsed data, or an endpoint returning a type different from what its base class/interface expects.

- Logic / null / control-flow defects:
  - Unconditional method calls on values that can be null/nil.
  - Indexing into query results without checking for empty result sets.
  - Controllers that retrieve a record by ID and call methods on it without verifying it exists.
  - Case-sensitivity mismatches where one side is normalized and the other is not.
  - Presence-check truthiness defects where `0`, `0.0`, empty string, or another falsy valid value should be allowed.
  - Optional/presence unwrapping without a guard.
  - Collection-order assumptions such as zipping keys with values and assuming positional alignment.
  - Hardcoded empty/fallback data passed to components that should display real data.
  - Analytics/logging/metric events emitted before a guard that can return early.
  - Logger context bypassed by using a bare logger instead of the enriched one.
  - Inconsistent metric labels or recorders across branches demonstrated by the diff.

Do not report:
- Git submodules, file modes, repository structure, or build/CI configuration changes as code defects.
- Issues that rely on assumptions about framework/runtime internals unless the diff itself demonstrates the failure.
- Removed public methods or potential breaking changes for callers outside the diff unless you can show a concrete signature mismatch within the changed files.
- Hypothetical SQL, serialization, or external-API runtime errors that are not directly caused by a removed guard or an obvious type mismatch in the changed code.
- Generic type widening, style, or maintainability concerns unless they directly create a concrete defect introduced by the diff.
- Performance or rate-limit regressions, extra network round-trips, or dead code that does not change observable runtime behavior.
- Behavior changes that only soften validation, remove logging/guards, or omit additional business-logic steps unless you can show they directly cause a runtime failure or data loss in the changed code.
- Adjacent plausible bugs that are not the exact introduced defect the diff creates.

Emit at most 10 findings, and only if each is a high-confidence concrete introduced defect visible in the diff. If you are not confident, omit it. It is better to output zero findings than an unsupported finding. Do not emit a finding solely because it looks like a common anti-pattern; the diff must demonstrate the concrete failure.

Respond with only JSON:
{"findings":[{"text":"specific issue and impact","path":"changed/file.ext","line":123,"severity":"critical|high|medium|low","category":"bug|security|concurrency|data|api|perf|test_gap|doc_defect|style"}]}"""


@dataclass(frozen=True)
class ReviewShard:
    """A self-contained portion of a large PR for an isolated reviewer call."""

    changed_files: list[dict]
    diff_text: str


_DIFF_FILE = re.compile(r"^diff --git a/(.*?) b/(.*?)$", re.M)
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _conf(name: str, default):
    return getattr(settings, name, default)


def build_review_prompt(*, pr_title: str, pr_url: str, changed_files: list[dict], diff_text: str) -> str:
    diff_limit = int(_conf("PRCHECK_REVIEW_DIFF_LIMIT", 120000))
    return (
        f"PR title: {pr_title}\n"
        f"PR URL: {pr_url}\n\n"
        f"Changed files:\n{json.dumps(changed_files, indent=2)}\n\n"
        f"Diff:\n{_fence(diff_text, diff_limit)}\n"
    )


def build_review_shards(changed_files: list[dict], diff_text: str) -> list[ReviewShard]:
    """Split a large PR into balanced, file-aware reviewer inputs.

    Every changed file is represented before any file gets extra context, so a
    huge single prefill is avoided without the blind spots of only taking the
    start and end of a large diff.
    """
    threshold_lines = _positive("PRCHECK_REVIEW_PARALLEL_THRESHOLD_LINES", 800)
    max_shards = _positive("PRCHECK_REVIEW_MAX_SHARDS", 4)
    if diff_text.count("\n") + 1 < threshold_lines or max_shards <= 1:
        return [ReviewShard(changed_files=changed_files, diff_text=diff_text)]

    blocks = _diff_blocks(diff_text)
    if len(blocks) == 1:
        blocks = _split_large_file_block(blocks[0], max_shards)
    if len(blocks) <= 1:
        return [ReviewShard(changed_files=changed_files, diff_text=diff_text)]

    total_limit = _positive("PRCHECK_REVIEW_TOTAL_DIFF_LIMIT", _positive("PRCHECK_REVIEW_DIFF_LIMIT", 120_000))
    blocks = _fit_blocks_to_total_limit(blocks, total_limit)
    shard_count = min(max_shards, len(blocks))

    # Largest-first bin packing keeps the slowest reviewer close to the others.
    bins: list[list[tuple[int, str | None, str]]] = [[] for _ in range(shard_count)]
    sizes = [0] * shard_count
    for index, (path, text) in sorted(enumerate(blocks), key=lambda item: (-len(item[1][1]), item[0])):
        destination = min(range(shard_count), key=lambda i: (sizes[i], i))
        bins[destination].append((index, path, text))
        sizes[destination] += len(text)

    by_path: dict[str, list[dict]] = {}
    for item in changed_files:
        path = str(item.get("path") or "")
        if path:
            by_path.setdefault(path, []).append(item)

    shards: list[tuple[int, ReviewShard]] = []
    assigned_paths: set[str] = set()
    for entries in bins:
        entries.sort(key=lambda item: item[0])
        paths = [path for _, path, _ in entries if path]
        assigned_paths.update(paths)
        unique_paths = list(dict.fromkeys(paths))
        shard_files = [file for path in unique_paths for file in by_path.get(path, [])]
        shards.append((entries[0][0], ReviewShard(
            changed_files=shard_files,
            diff_text="".join(text for _, _, text in entries),
        )))

    # Binary / rename-only paths may lack a standard diff header; keep them visible.
    unassigned = [item for item in changed_files if str(item.get("path") or "") not in assigned_paths]
    if unassigned and shards:
        first = shards[0][1]
        shards[0] = (shards[0][0], ReviewShard(
            changed_files=[*first.changed_files, *unassigned],
            diff_text=first.diff_text,
        ))
    return [shard for _, shard in sorted(shards, key=lambda item: item[0])]


def merge_review_results(results: list[dict | None]) -> dict | None:
    """Deduplicate concurrent shard findings and retain the highest priorities."""
    non_empty = [result for result in results if result is not None]
    if not non_empty:
        return None
    merged = dict(non_empty[0])
    findings: list[tuple[int, dict]] = []
    for result in non_empty:
        findings.extend(
            (position, finding)
            for position, finding in enumerate(result.get("findings") or [])
            if isinstance(finding, dict)
        )

    seen: set[tuple[str, int | str, str]] = set()
    unique: list[tuple[int, dict]] = []
    for position, finding in findings:
        fingerprint = (
            str(finding.get("path") or ""),
            finding.get("line") or "",
            re.sub(r"\s+", " ", str(finding.get("text") or "")).strip().casefold(),
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append((position, finding))
    unique.sort(key=lambda item: (
        _SEVERITY_RANK.get(str(item[1].get("severity") or "").lower(), 4),
        item[0],
    ))
    limit = _positive("PRCHECK_REVIEW_MAX_FINDINGS", 40)
    merged["findings"] = [finding for _, finding in unique[:limit]]
    merged["_review_shards"] = len(non_empty)
    return merged


def run_reviewer(
    completer: Completer,
    budget: Budget,
    *,
    pr_title: str,
    pr_url: str,
    changed_files: list[dict],
    diff_text: str,
    max_tokens: int | None = None,
) -> dict | None:
    """Run one reviewer call over a single shard's diff."""
    prompt = build_review_prompt(
        pr_title=pr_title, pr_url=pr_url, changed_files=changed_files, diff_text=diff_text,
    )
    system_prompt = DEFAULT_REVIEW_PROMPT
    result = structured_call(
        completer,
        budget,
        session="reviewer",
        system_prompt=system_prompt,
        prompt=prompt,
        schema=REVIEWER_SCHEMA,
        stage="review",
        max_tokens=max_tokens,
    )
    if result is not None:
        result["_prompt_sha256"] = hashlib.sha256(system_prompt.encode()).hexdigest()
        result["_input_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
        result["_reviewer_backend"] = resolve_backend()
    return result


def _fence(text: str, limit: int) -> str:
    if limit <= 0:
        return "<untrusted>\n\n</untrusted>"
    if len(text) > limit:
        half = limit // 2
        text = text[:half] + "\n[... truncated ...]\n" + text[-half:]
    return f"<untrusted>\n{text}\n</untrusted>"


def _positive(name: str, default: int) -> int:
    try:
        value = int(_conf(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _diff_blocks(diff_text: str) -> list[tuple[str | None, str]]:
    matches = list(_DIFF_FILE.finditer(diff_text))
    if not matches:
        return [(None, diff_text)]
    blocks: list[tuple[str | None, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(diff_text)
        blocks.append((match.group(2), diff_text[match.start():end]))
    return blocks


def _split_large_file_block(block: tuple[str | None, str], count: int) -> list[tuple[str | None, str]]:
    path, text = block
    if count <= 1:
        return [block]
    hunk_start = text.find("@@")
    header = text[:hunk_start] if hunk_start >= 0 else ""
    body = text[hunk_start:] if hunk_start >= 0 else text
    target = max(1, (len(body) + count - 1) // count)
    pieces: list[str] = []
    current: list[str] = []
    current_size = 0
    for line in body.splitlines(keepends=True):
        if current and current_size + len(line) > target:
            pieces.append(header + "".join(current))
            current, current_size = [], 0
        current.append(line)
        current_size += len(line)
    if current:
        pieces.append(header + "".join(current))
    return [(path, piece) for piece in pieces] or [block]


def _fit_blocks_to_total_limit(blocks: list[tuple[str | None, str]], total_limit: int):
    total = sum(len(text) for _, text in blocks)
    if total <= total_limit:
        return blocks
    per_block = max(1, total_limit // len(blocks))
    return [(path, _clip_text(text, per_block)) for path, text in blocks]


def _clip_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = max(1, (limit - len("\n[... truncated ...]\n")) // 2)
    return text[:half] + "\n[... truncated ...]\n" + text[-half:]
