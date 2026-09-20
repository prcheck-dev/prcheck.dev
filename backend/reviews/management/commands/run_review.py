"""Run a PR review from the command line (synchronously) and print the result.

    python manage.py run_review owner/name 123
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from reviews.models import Review
from reviews.services import run_review


class Command(BaseCommand):
    help = "Run a code review against a GitHub pull request."

    def add_arguments(self, parser):
        parser.add_argument("repo", help="Repository in owner/name form")
        parser.add_argument("pr_number", type=int, help="Pull request number")

    def handle(self, *args, **options):
        repo = options["repo"]
        if "/" not in repo:
            raise CommandError("repo must be in 'owner/name' form")
        review = Review.objects.create(repo=repo, pr_number=options["pr_number"], trigger="cli")
        self.stdout.write(f"Running review {review.pk} for {repo}#{options['pr_number']} ...")
        review = run_review(review.pk)

        self.stdout.write(self.style.SUCCESS(f"status={review.status} verdict={review.verdict}"))
        if review.error:
            self.stderr.write(self.style.ERROR(review.error))
        payload = {
            "verdict": review.verdict,
            "conditions": review.conditions,
            "degraded": review.degraded,
            "findings": [
                {"severity": f.severity, "category": f.category,
                 "path": f.path, "line": f.line, "text": f.text}
                for f in review.findings.all()
            ],
            "usage": review.usage,
        }
        self.stdout.write(json.dumps(payload, indent=2))
