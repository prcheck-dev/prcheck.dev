"""Review orchestration: fetch a PR, review it, verdict it, persist, publish.

Mirrors shipwright's pipeline shape (map -> review -> adversarial -> verdict ->
publish) trimmed to a code-review agent. Reviewer shards run concurrently on a
thread pool sharing one budget; the adversarial pass can only tighten the
verdict on substantiated evidence. Every finding is grounded against the diff
in code before it is stored, so the model cannot publish off-diff noise.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import connection

from . import adversary as adversary_mod
from . import github_app
from . import github_client as gh
from .budget import Budget, BudgetExhausted
from .deep_review import run_deep_review
from .diffs import filter_reviewable, line_map, number_diff
from .findings import dedupe_findings, ground_findings, sort_findings
from .llm import Completer
from .models import Finding, Review
from .reviewer import build_review_shards, merge_review_results, run_reviewer
from .verdict import APPROVE, APPROVE_COND, compute_verdict

LOGGER = logging.getLogger("reviews.services")


class ReviewConfigError(RuntimeError):
    """Raised when the server is missing configuration needed to run a review."""


def _conf(name: str, default=None):
    return getattr(settings, name, default)


def run_review(review_id: int) -> Review:
    """Execute a review synchronously and persist the outcome."""
    review = Review.objects.get(pk=review_id)
    review.status = Review.Status.RUNNING
    review.save(update_fields=["status", "updated_at"])

    # Prefer acting as the installed GitHub App (comments post as the bot);
    # fall back to a static PAT when the App is not configured.
    if github_app.is_configured():
        try:
            token = github_app.token_for_repo(review.repo)
        except gh.GitHubAPIError as exc:
            return _fail(review, f"GitHub App auth failed: {exc}")
    else:
        token = _conf("PRCHECK_GITHUB_TOKEN")
        if not token:
            return _fail(review, "No GitHub credentials configured (App or PRCHECK_GITHUB_TOKEN).")

    api = gh.GitHubAPI(
        repo=review.repo, token=token,
        base=_conf("PRCHECK_GITHUB_API_URL", "https://api.github.com"),
    )
    budget = Budget()
    completer = Completer()

    try:
        snapshot = gh.fetch_pull_snapshot(api, review.pr_number)
    except gh.GitHubAPIError as exc:
        return _fail(review, f"Could not load PR: {exc}")

    review.pr_title = snapshot.title
    review.pr_url = snapshot.url
    review.head_sha = snapshot.head_sha
    review.additions = snapshot.additions
    review.deletions = snapshot.deletions
    review.save(update_fields=["pr_title", "pr_url", "head_sha", "additions", "deletions", "updated_at"])

    # Show an in-progress check on the PR while the review runs.
    check_run_id = gh.create_check_run(api, snapshot.head_sha) if _conf("PRCHECK_ENABLE_CHECKS", True) else None
    ci_evidence = (
        gh.fetch_ci_evidence(api, snapshot.head_sha)
        if _conf("PRCHECK_CI_GATE_APPROVAL", True) else None
    )

    changed_files, review_diff, skipped = filter_reviewable(
        snapshot.changed_files, snapshot.diff_text,
        extra_globs=_conf("PRCHECK_REVIEW_IGNORE_GLOBS", ()) or (),
    )
    if skipped:
        LOGGER.info("reviews_skipped_files review=%s count=%d", review.pk, len(skipped))
    if not review_diff.strip():
        return _finalize(review, api, snapshot, findings=[], degraded=False,
                         ci_evidence=ci_evidence, check_run_id=check_run_id)

    project_guidance = ""
    if _conf("PRCHECK_REPO_GUIDANCE", True):
        project_guidance = gh.fetch_repository_guidance(
            api,
            ref=snapshot.base_sha or snapshot.head_sha,
            changed_paths=[item.get("path", "") for item in changed_files],
            max_bytes=int(_conf("PRCHECK_REPO_GUIDANCE_BYTES", 24_000)),
        )

    if str(_conf("PRCHECK_REVIEW_MODE", "fast")).lower() == "deep":
        merged = _run_deep(api, completer, budget, review, snapshot, changed_files, review_diff, project_guidance)
    else:
        merged = _run_fast(completer, budget, review, snapshot, changed_files, review_diff, project_guidance)
    degraded = merged is None
    lines_by_path = line_map(review_diff)
    findings = ground_findings(list(merged.get("findings") or []) if merged else [], lines_by_path)

    # -- adversarial second pass (can only tighten) ------------------------- #
    if _conf("PRCHECK_ENABLE_ADVERSARY", True):
        try:
            adv = adversary_mod.run_adversary(
                completer, budget, diff_text=review_diff, review={"findings": findings},
            )
        except BudgetExhausted:
            adv = None
        findings = _merge_adversary(findings, adversary_mod.verified_blockers(adv))

    return _finalize(
        review, api, snapshot,
        findings=sort_findings(dedupe_findings(findings)), degraded=degraded,
        usage=budget.as_dict(), ci_evidence=ci_evidence, check_run_id=check_run_id,
    )


def _merge_adversary(findings: list[dict], blockers: list[dict]) -> list[dict]:
    """Fold verified adversary blockers into the findings.

    A blocker at the location of an existing finding corroborates it and
    escalates it to critical; otherwise it is added, so a blocked PR always
    shows the finding that blocked it.
    """
    findings = [dict(f) for f in findings]
    for blocker in blockers:
        near = next((
            f for f in findings
            if f.get("path") == blocker["path"] and abs(int(f.get("line") or 0) - blocker["line"]) <= 3
        ), None)
        if near is not None:
            near["severity"] = "critical"
        else:
            findings.append(blocker)
    return findings


def _run_fast(completer, budget, review, snapshot, changed_files, review_diff, project_guidance=""):
    """Precision-tuned single/size-sharded reviewer (the original path)."""
    shards = build_review_shards(changed_files, number_diff(review_diff))
    max_workers = min(len(shards), int(_conf("PRCHECK_REVIEW_MAX_WORKERS", 4)))

    def _review_shard(shard):
        return run_reviewer(
            completer, budget, pr_title=snapshot.title, pr_url=snapshot.url,
            changed_files=shard.changed_files, diff_text=shard.diff_text,
            project_guidance=project_guidance,
        )

    try:
        if len(shards) == 1:
            results = [_review_shard(shards[0])]
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                results = list(pool.map(_review_shard, shards))
    except BudgetExhausted as exc:
        LOGGER.warning("reviews_budget_exhausted review=%s %s", review.pk, exc)
        results = []
    return merge_review_results(results)


def _run_deep(api, completer, budget, review, snapshot, changed_files, review_diff, project_guidance=""):
    """High-recall per-file issue-list generation + verification."""
    # Fetch the full text of changed files (bounded) so the generator can reason
    # about null-derefs, types, and interfaces beyond the diff hunks.
    file_contents = {}
    if api.enabled and snapshot.head_sha:
        max_files = int(_conf("PRCHECK_DEEP_MAX_FILES", 40))
        for item in changed_files[:max_files]:
            path = item.get("path")
            if path and item.get("status") not in {"removed", "deleted"}:
                file_contents[path] = api.file_content(path, snapshot.head_sha)
    related_definitions = {}
    if _conf("PRCHECK_DEEP_RELATED_DEFINITIONS", True):
        related_definitions = gh.fetch_related_definitions(
            api,
            ref=snapshot.head_sha,
            changed_files=changed_files,
            file_contents=file_contents,
            max_files=int(_conf("PRCHECK_DEEP_MAX_FILES", 40)),
            max_definitions=int(_conf("PRCHECK_DEEP_MAX_RELATED_DEFINITIONS", 12)),
            max_bytes=int(_conf("PRCHECK_DEEP_RELATED_DEFINITION_BYTES", 16_000)),
        )
    try:
        return run_deep_review(
            completer, budget, pr_title=snapshot.title, pr_url=snapshot.url,
            changed_files=changed_files, diff_text=review_diff,
            file_contents=file_contents,
            related_definitions=related_definitions,
            project_guidance=project_guidance,
        )
    except BudgetExhausted as exc:
        LOGGER.warning("reviews_budget_exhausted review=%s %s", review.pk, exc)
        return None


def _finalize(review, api, snapshot, *, findings, degraded,
              usage=None, ci_evidence=None, check_run_id=None) -> Review:
    verdict, conditions = compute_verdict(findings, degraded=degraded)

    if ci_evidence and verdict in {APPROVE, APPROVE_COND}:
        failed = ci_evidence.get("failed") or []
        pending = ci_evidence.get("pending") or []
        if failed:
            conditions.append("CI checks failing: " + ", ".join(failed[:5]))
        if pending:
            conditions.append("CI checks pending: " + ", ".join(pending[:5]))
        if failed or pending:
            verdict = APPROVE_COND

    Finding.objects.filter(review=review).delete()
    Finding.objects.bulk_create([
        Finding(
            review=review,
            text=str(f.get("text", ""))[:5000],
            path=str(f.get("path", ""))[:1024],
            line=int(f.get("line") or 0),
            severity=str(f.get("severity", "low")),
            category=str(f.get("category", "bug")),
            suggestion=str(f.get("suggestion") or "")[:5000],
        )
        for f in findings
    ])

    review.verdict = verdict
    review.conditions = conditions
    review.degraded = degraded
    review.usage = usage or {}
    review.status = Review.Status.COMPLETED
    review.save(update_fields=["verdict", "conditions", "degraded", "usage", "status", "updated_at"])

    blob_base = _blob_base(snapshot)
    if _conf("PRCHECK_ENABLE_CHECKS", True):
        gh.complete_check_run(api, check_run_id, verdict, conditions, findings, blob_base=blob_base)

    if _conf("PRCHECK_PUBLISH_REVIEWS", False) and api.enabled:
        if _superseded(review):
            # A later push already started a newer review; publishing this one
            # would overwrite fresher results with stale ones.
            LOGGER.info("reviews_publish_skipped_superseded review=%s", review.pk)
        else:
            _publish(review, api, snapshot, findings, verdict, conditions, blob_base)

    LOGGER.info(
        "reviews_finished review=%s repo=%s pr=%s verdict=%s findings=%d degraded=%s",
        review.pk, review.repo, review.pr_number, verdict, len(findings), degraded,
    )
    return review


def _blob_base(snapshot) -> str:
    if "/pull/" not in snapshot.url or not snapshot.head_sha:
        return ""
    return f"{snapshot.url.split('/pull/')[0]}/blob/{snapshot.head_sha}"


def _superseded(review: Review) -> bool:
    return Review.objects.filter(
        repo=review.repo, pr_number=review.pr_number, pk__gt=review.pk,
    ).exists()


def _publish(review, api, snapshot, findings, verdict, conditions, blob_base="") -> None:
    try:
        gh.upsert_summary_comment(
            api, review.pr_number,
            gh.render_summary(verdict, conditions, findings, blob_base=blob_base),
        )
        gh.publish_inline_comments(
            api, review.pr_number, snapshot.head_sha, snapshot.diff_text, findings,
            limit=int(_conf("PRCHECK_MAX_INLINE_COMMENTS", 15)),
            min_severity=str(_conf("PRCHECK_INLINE_MIN_SEVERITY", "medium")).lower(),
        )
        review.published = True
        review.save(update_fields=["published", "updated_at"])
    except gh.GitHubAPIError as exc:
        LOGGER.warning("reviews_publish_failed review=%s %s", review.pk, exc)


def _fail(review: Review, message: str) -> Review:
    review.status = Review.Status.FAILED
    review.error = message
    review.save(update_fields=["status", "error", "updated_at"])
    LOGGER.warning("reviews_failed review=%s %s", review.pk, message)
    return review


def run_review_in_background(review_id: int) -> None:
    """Run a review off the request thread with its own DB connection."""
    def _worker():
        try:
            run_review(review_id)
        except Exception:  # never let the worker thread die silently
            LOGGER.exception("reviews_worker_crashed review=%s", review_id)
            try:
                review = Review.objects.get(pk=review_id)
                if review.status != Review.Status.COMPLETED:
                    _fail(review, "internal error during review")
            except Exception:
                pass
        finally:
            connection.close()  # threads must not reuse the main connection

    threading.Thread(target=_worker, name=f"review-{review_id}", daemon=True).start()
