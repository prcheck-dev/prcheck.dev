"""API endpoints for triggering and reading PR reviews, plus a GitHub webhook."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from .models import Review
from .serializers import (
    CreateReviewSerializer,
    ReviewListSerializer,
    ReviewSerializer,
)
from .services import run_review_in_background

LOGGER = logging.getLogger("reviews.views")


class ReviewThrottle(ScopedRateThrottle):
    scope = "reviews"


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([ReviewThrottle])
def reviews(request):
    """GET: list reviews. POST: trigger a new review for a PR."""
    if request.method == "GET":
        qs = Review.objects.all()
        repo = request.query_params.get("repo")
        if repo:
            qs = qs.filter(repo=repo)
        pr = request.query_params.get("pr_number")
        if pr and pr.isdigit():
            qs = qs.filter(pr_number=int(pr))
        return Response(ReviewListSerializer(qs[:100], many=True).data)

    serializer = CreateReviewSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    review = Review.objects.create(
        repo=serializer.validated_data["repo"],
        pr_number=serializer.validated_data["pr_number"],
        requested_by=request.user,
        trigger="api",
    )
    run_review_in_background(review.pk)
    return Response(ReviewSerializer(review).data, status=status.HTTP_202_ACCEPTED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def review_detail(request, pk: int):
    try:
        review = Review.objects.get(pk=pk)
    except Review.DoesNotExist:
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(ReviewSerializer(review).data)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def github_webhook(request):
    """Handle GitHub pull_request webhooks; trigger a review on open/sync/reopen.

    Requires PRCHECK_GITHUB_WEBHOOK_SECRET and verifies the HMAC signature.
    """
    secret = getattr(settings, "PRCHECK_GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        return Response({"detail": "Webhook not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    signature = request.META.get("HTTP_X_HUB_SIGNATURE_256", "")
    expected = "sha256=" + hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return Response({"detail": "Invalid signature."}, status=status.HTTP_401_UNAUTHORIZED)

    event = request.META.get("HTTP_X_GITHUB_EVENT")
    if event == "ping":
        return Response({"ok": True})

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return Response({"detail": "Invalid payload."}, status=status.HTTP_400_BAD_REQUEST)

    # Comment command: `/prcheck` (or `/prcheck review`) on a PR re-runs the review.
    if event == "issue_comment":
        return _handle_comment_command(payload)
    if event != "pull_request":
        return Response({"ignored": True})

    if payload.get("action") not in {"opened", "synchronize", "reopened", "ready_for_review"}:
        return Response({"ignored": True})

    repo = str((payload.get("repository") or {}).get("full_name") or "")
    pr_number = (payload.get("pull_request") or {}).get("number")
    if not repo or not isinstance(pr_number, int):
        return Response({"detail": "Missing repo or PR number."}, status=status.HTTP_400_BAD_REQUEST)

    review = Review.objects.create(repo=repo, pr_number=pr_number, trigger="webhook")
    run_review_in_background(review.pk)
    return Response({"review_id": review.pk}, status=status.HTTP_202_ACCEPTED)


def _handle_comment_command(payload: dict):
    """Run a review when a PR comment invokes the command (e.g. `/prcheck`)."""
    if payload.get("action") != "created":
        return Response({"ignored": True})
    issue = payload.get("issue") or {}
    if "pull_request" not in issue:  # only PR conversations, not plain issues
        return Response({"ignored": True})

    prefix = str(getattr(settings, "PRCHECK_COMMAND_PREFIX", "/prcheck")).lower()
    body = str((payload.get("comment") or {}).get("body") or "").strip().lower()
    if body != prefix and not body.startswith(prefix + " "):
        return Response({"ignored": True})

    # Only "review" (or the bare prefix) is a valid command today.
    subcommand = body[len(prefix):].strip().split()
    if subcommand and subcommand[0] != "review":
        return Response({"detail": "Unknown command."}, status=status.HTTP_400_BAD_REQUEST)

    repo = str((payload.get("repository") or {}).get("full_name") or "")
    pr_number = issue.get("number")
    if not repo or not isinstance(pr_number, int):
        return Response({"detail": "Missing repo or PR number."}, status=status.HTTP_400_BAD_REQUEST)

    review = Review.objects.create(repo=repo, pr_number=pr_number, trigger="command")
    run_review_in_background(review.pk)
    return Response({"review_id": review.pk}, status=status.HTTP_202_ACCEPTED)
