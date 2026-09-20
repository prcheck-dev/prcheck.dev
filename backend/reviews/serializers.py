import re

from rest_framework import serializers

from .models import Finding, Review

_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


class FindingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Finding
        fields = ("id", "text", "path", "line", "severity", "category")


class ReviewSerializer(serializers.ModelSerializer):
    findings = FindingSerializer(many=True, read_only=True)

    class Meta:
        model = Review
        fields = (
            "id", "repo", "pr_number", "status", "verdict", "conditions",
            "pr_title", "pr_url", "head_sha", "additions", "deletions",
            "degraded", "error", "usage", "published", "trigger",
            "created_at", "updated_at", "findings",
        )
        read_only_fields = fields


class ReviewListSerializer(serializers.ModelSerializer):
    """Compact representation for listings (no full findings payload)."""

    finding_count = serializers.IntegerField(source="findings.count", read_only=True)

    class Meta:
        model = Review
        fields = (
            "id", "repo", "pr_number", "status", "verdict",
            "pr_title", "pr_url", "degraded", "finding_count", "created_at",
        )
        read_only_fields = fields


class CreateReviewSerializer(serializers.Serializer):
    """Input for triggering a review of a pull request."""

    repo = serializers.CharField(max_length=255, help_text="owner/name")
    pr_number = serializers.IntegerField(min_value=1)

    def validate_repo(self, value: str) -> str:
        value = value.strip()
        if not _REPO_RE.match(value):
            raise serializers.ValidationError("repo must be in 'owner/name' form.")
        return value
