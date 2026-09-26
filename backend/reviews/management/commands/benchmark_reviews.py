"""Run the review pipeline over benchmark PRs offline and write the findings.

    PRCHECK_GITHUB_TOKEN=... python manage.py benchmark_reviews \\
        ../experiments/golden_comments/sentry.json --out sentry_run.json

Reads golden-comment files (``[{"pr_title", "url", "comments"}]``), reviews
each PR in-process, and writes the findings in the same schema so the judge can
score them. Nothing is written to GitHub: publishing, check runs and the CI gate
are forced off, and the GitHub App is bypassed in favour of the read-only token.
Existing entries in ``--out`` are kept, so a crashed run resumes where it
stopped; failed or degraded PRs are retried on the next invocation.

Each entry also records ``unselected``: the findings that reached PR-level
selection, so one run scores the pipeline both with and without it.
``--reselect-from`` reruns only the selection stage over a previous run's
``unselected`` findings, so selector changes are measured on identical
generations without paying for generation again.
"""
from __future__ import annotations

import json
import threading
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.test.utils import override_settings

from reviews import github_client as gh
from reviews import services
from reviews.budget import Budget
from reviews.diffs import filter_reviewable, number_diff
from reviews.llm import Completer
from reviews.models import Review
from reviews.selection import select_findings
from reviews.services import run_review

OFFLINE_SETTINGS = dict(
    PRCHECK_PUBLISH_REVIEWS=False,
    PRCHECK_ENABLE_CHECKS=False,
    PRCHECK_CI_GATE_APPROVAL=False,
    PRCHECK_GITHUB_APP_ID="",
    PRCHECK_GITHUB_APP_PRIVATE_KEY="",
)


def _parse_url(url: str) -> tuple[str, int]:
    parts = str(url).rstrip("/").split("/")
    if len(parts) < 4 or parts[-2] != "pull" or not parts[-1].isdigit():
        raise CommandError(f"not a pull request URL: {url}")
    return f"{parts[-4]}/{parts[-3]}", int(parts[-1])


def _comment(text: str, suggestion: str = "") -> str:
    # Mirrors the inline comment body, which is what the benchmark extracts.
    return f"{text}\n\nSuggested fix: {suggestion}" if suggestion else text


def _row(text, suggestion, severity, category, path, line) -> dict:
    return {"comment": _comment(text, suggestion), "severity": str(severity).capitalize(),
            "category": category, "path": path, "line": line}


class Command(BaseCommand):
    help = "Review benchmark PRs offline and write findings in golden-comment format."

    def add_arguments(self, parser):
        parser.add_argument("golden", nargs="+", help="Golden-comment JSON file(s)")
        parser.add_argument("--out", required=True, help="Output JSON path (resumable)")
        parser.add_argument("--prs", default="", help="Comma-separated 1-based PR indices per file")
        parser.add_argument("--mode", choices=["fast", "deep"], default="deep")
        parser.add_argument("--top-k", type=int, default=None, help="Override PRCHECK_REVIEW_TOP_K")
        parser.add_argument("--no-select", action="store_true", help="Disable PR-level selection")
        parser.add_argument("--concurrency", type=int, default=2, help="PRs reviewed at once")
        parser.add_argument("--reselect-from", default="",
                            help="Rerun only PR-level selection over this run's `unselected` findings")

    def handle(self, *args, **options):
        out = Path(options["out"])
        results = {e["url"]: e for e in json.loads(out.read_text())} if out.exists() else {}
        wanted = {int(n) for n in options["prs"].split(",") if n.strip()}

        entries = []
        for golden_path in options["golden"]:
            for index, entry in enumerate(json.loads(Path(golden_path).read_text()), 1):
                if wanted and index not in wanted:
                    continue
                done = results.get(entry["url"])
                if done and done.get("status") == "completed" and not done.get("degraded"):
                    continue
                entries.append(entry)
        self.stdout.write(f"{len(entries)} PR(s) to review ({len(results)} already in {out.name})")

        overrides = dict(OFFLINE_SETTINGS, PRCHECK_REVIEW_MODE=options["mode"])
        if options["top_k"] is not None:
            overrides["PRCHECK_REVIEW_TOP_K"] = options["top_k"]
        if options["no_select"]:
            overrides["PRCHECK_REVIEW_SELECT"] = False

        lock = threading.Lock()
        # Reviews run one per worker thread, so a thread-local holds each
        # review's pre-selection findings.
        captured = threading.local()
        real_select = services.select_findings

        def _capturing_select(*args, **kwargs):
            captured.findings = list(kwargs.get("findings") or [])
            return real_select(*args, **kwargs)

        previous = (
            {e["url"]: e for e in json.loads(Path(options["reselect_from"]).read_text())}
            if options["reselect_from"] else None
        )

        def _reselect(entry):
            repo, number = _parse_url(entry["url"])
            source = previous.get(entry["url"])
            if source is None or "unselected" not in source:
                return {"pr_title": entry.get("pr_title", ""), "url": entry["url"], "comments": [],
                        "status": "failed", "error": "no unselected findings to reselect", "degraded": True}
            api = gh.GitHubAPI(repo=repo, token=settings.PRCHECK_GITHUB_TOKEN)
            snapshot = gh.fetch_pull_snapshot(api, number)
            _, review_diff, _ = filter_reviewable(snapshot.changed_files, snapshot.diff_text)
            findings = []
            for row in source["unselected"]:
                text, _, suggestion = row["comment"].partition("\n\nSuggested fix: ")
                findings.append({"text": text, "suggestion": suggestion, "path": row["path"],
                                 "line": row["line"], "severity": row["severity"].lower(),
                                 "category": row["category"]})
            chosen = select_findings(
                Completer(), Budget(), pr_title=snapshot.title, diff_text=number_diff(review_diff),
                findings=findings, top_k=int(getattr(settings, "PRCHECK_REVIEW_TOP_K", 8)),
            )
            by_id = {id(f): row for f, row in zip(findings, source["unselected"])}
            return {**source, "comments": [by_id[id(f)] for f in chosen]}

        def _review(entry):
            if previous is not None:
                result = _reselect(entry)
                with lock:
                    results[entry["url"]] = result
                    out.write_text(json.dumps(list(results.values()), indent=2))
                self.stdout.write(f"reselected {len(result['comments']):2} finding(s) {entry['url']}")
                return
            repo, number = _parse_url(entry["url"])
            result = {"pr_title": entry.get("pr_title", ""), "url": entry["url"], "comments": []}
            captured.findings = None
            try:
                review = run_review(
                    Review.objects.create(repo=repo, pr_number=number, trigger="benchmark").pk
                )
                result.update(
                    status=review.status, error=review.error, degraded=review.degraded,
                    verdict=review.verdict, usage=review.usage,
                    comments=[
                        _row(f.text, f.suggestion, f.severity, f.category, f.path, f.line)
                        for f in review.findings.all()
                    ],
                )
                if captured.findings is not None:
                    result["unselected"] = [
                        _row(f.get("text", ""), f.get("suggestion") or "", f.get("severity", ""),
                             f.get("category", ""), f.get("path", ""), f.get("line", 0))
                        for f in captured.findings
                    ]
            except Exception as exc:  # one broken PR must not abort the whole run
                result.update(status="failed", error=repr(exc), degraded=True)
            finally:
                connection.close()  # each worker thread owns its connection
            with lock:
                results[entry["url"]] = result
                out.write_text(json.dumps(list(results.values()), indent=2))
            self.stdout.write(
                f"{result['status']:9} {len(result['comments']):2} finding(s) "
                f"degraded={result['degraded']} {entry['url']}"
            )

        with override_settings(**overrides), \
             mock.patch.object(services, "select_findings", _capturing_select):
            with ThreadPoolExecutor(max_workers=max(1, options["concurrency"])) as pool:
                for future in [pool.submit(_review, e) for e in entries]:
                    future.result()
        self.stdout.write(self.style.SUCCESS(f"wrote {len(results)} PR(s) -> {out}"))
