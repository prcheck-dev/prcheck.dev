"""Persistence for review runs and their findings."""
from __future__ import annotations

from django.conf import settings
from django.db import models


class Review(models.Model):
    """One review run against a single pull request."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    class Verdict(models.TextChoices):
        BLOCK = "block", "Block"
        REQUEST_CHANGES = "request-changes", "Request changes"
        APPROVE_COND = "approve-with-conditions", "Approve with conditions"
        APPROVE = "approve", "Approve"

    repo = models.CharField(max_length=255, db_index=True)  # "owner/name"
    pr_number = models.PositiveIntegerField(db_index=True)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    verdict = models.CharField(max_length=32, choices=Verdict.choices, blank=True)
    conditions = models.JSONField(default=list, blank=True)

    pr_title = models.CharField(max_length=500, blank=True)
    pr_url = models.URLField(blank=True)
    head_sha = models.CharField(max_length=64, blank=True)

    additions = models.PositiveIntegerField(default=0)
    deletions = models.PositiveIntegerField(default=0)

    degraded = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    usage = models.JSONField(default=dict, blank=True)
    published = models.BooleanField(default=False)

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="reviews",
    )
    trigger = models.CharField(max_length=32, default="api")  # api | webhook | cli

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["repo", "pr_number", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.repo}#{self.pr_number} ({self.status})"


class Finding(models.Model):
    """A single concrete defect reported by the reviewer."""

    class Severity(models.TextChoices):
        CRITICAL = "critical", "Critical"
        HIGH = "high", "High"
        MEDIUM = "medium", "Medium"
        LOW = "low", "Low"

    review = models.ForeignKey(Review, on_delete=models.CASCADE, related_name="findings")
    text = models.TextField()
    path = models.CharField(max_length=1024)
    line = models.PositiveIntegerField()
    severity = models.CharField(max_length=16, choices=Severity.choices)
    category = models.CharField(max_length=32)
    suggestion = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    _SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

    class Meta:
        ordering = ("id",)

    def __str__(self) -> str:
        return f"[{self.severity}/{self.category}] {self.path}:{self.line}"
