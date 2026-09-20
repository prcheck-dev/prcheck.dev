"""Review orchestration: fetch a PR, review it, verdict it, persist, publish.

Mirrors shipwright's pipeline shape (map -> review -> adversarial -> verdict ->
publish) trimmed to a code-review agent. Reviewer shards run concurrently on a
thread pool sharing one budget; the adversarial pass can only tighten the
verdict on substantiated evidence.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import connection

from . import adversary as adversary_mod
from . import github_client as gh
from .budget import Budget, BudgetExhausted
from .llm import Completer
from .models import Finding, Review
from .reviewer import build_review_shards, merge_review_results, run_reviewer
from .verdict import BLOCK, compute_verdict

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

    token = _conf("PRCHECK_GITHUB_TOKEN")
    if not token:
        return _fail(review, "PRCHECK_GITHUB_TOKEN is not configured on the server.")

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

    if not snapshot.diff_text.strip():
        review.degraded = False
        return _finalize(review, api, snapshot, findings=[], degraded=False)

    # -- primary reviewer over shards (concurrent) -------------------------- #
    shards = build_review_shards(snapshot.changed_files, snapshot.diff_text)
    max_workers = min(len(shards), int(_conf("PRCHECK_REVIEW_MAX_WORKERS", 4)))

    def _review_shard(shard):
        return run_reviewer(
            completer, budget,
            pr_title=snapshot.title, pr_url=snapshot.url,
            changed_files=shard.changed_files, diff_text=shard.diff_text,
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

    merged = merge_review_results(results)
    degraded = merged is None
    findings = list(merged.get("findings") or []) if merged else []

    # -- adversarial second pass (verdict-gating only) ---------------------- #
    adversarial_verdict = None
    if _conf("PRCHECK_ENABLE_ADVERSARY", True) and snapshot.diff_text.strip():
        try:
            adv = adversary_mod.run_adversary(
                completer, budget, diff_text=snapshot.diff_text, review=merged,
            )
        except BudgetExhausted:
            adv = None
        if adv is not None:
            adversarial_verdict = adv.get("verdict")

    return _finalize(
        review, api, snapshot,
        findings=findings, degraded=degraded,
        adversarial_verdict=adversarial_verdict, usage=budget.as_dict(),
    )


def _finalize(review, api, snapshot, *, findings, degraded,
              adversarial_verdict=None, usage=None) -> Review:
    verdict, conditions = compute_verdict(findings, degraded=degraded)
    # The adversarial pass can only tighten the verdict.
    if adversarial_verdict == "BLOCK":
        verdict = BLOCK

    Finding.objects.filter(review=review).delete()
    Finding.objects.bulk_create([
        Finding(
            review=review,
            text=str(f.get("text", ""))[:5000],
            path=str(f.get("path", ""))[:1024],
            line=int(f.get("line") or 0),
            severity=str(f.get("severity", "low")),
            category=str(f.get("category", "bug")),
        )
        for f in findings
    ])

    review.verdict = verdict
    review.conditions = conditions
    review.degraded = degraded
    review.usage = usage or {}
    review.status = Review.Status.COMPLETED
    review.save(update_fields=["verdict", "conditions", "degraded", "usage", "status", "updated_at"])

    if _conf("PRCHECK_PUBLISH_REVIEWS", False) and api.enabled:
        _publish(review, api, snapshot, findings, verdict, conditions)

    LOGGER.info(
        "reviews_finished review=%s repo=%s pr=%s verdict=%s findings=%d degraded=%s",
        review.pk, review.repo, review.pr_number, verdict, len(findings), degraded,
    )
    return review


def _publish(review, api, snapshot, findings, verdict, conditions) -> None:
    try:
        gh.upsert_summary_comment(
            api, review.pr_number, gh.render_summary(verdict, conditions, findings),
        )
        gh.publish_inline_comments(
            api, review.pr_number, snapshot.head_sha, snapshot.diff_text, findings,
            limit=int(_conf("PRCHECK_MAX_INLINE_COMMENTS", 40)),
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
