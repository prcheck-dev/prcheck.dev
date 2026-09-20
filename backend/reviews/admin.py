from django.contrib import admin

from .models import Finding, Review


class FindingInline(admin.TabularInline):
    model = Finding
    extra = 0
    readonly_fields = ("text", "path", "line", "severity", "category", "created_at")
    can_delete = False


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ("id", "repo", "pr_number", "status", "verdict", "degraded", "published", "created_at")
    list_filter = ("status", "verdict", "degraded", "published", "trigger")
    search_fields = ("repo", "pr_title", "head_sha")
    readonly_fields = (
        "repo", "pr_number", "status", "verdict", "conditions", "pr_title", "pr_url",
        "head_sha", "additions", "deletions", "degraded", "error", "usage", "published",
        "requested_by", "trigger", "created_at", "updated_at",
    )
    inlines = [FindingInline]
    date_hierarchy = "created_at"


@admin.register(Finding)
class FindingAdmin(admin.ModelAdmin):
    list_display = ("id", "review", "severity", "category", "path", "line")
    list_filter = ("severity", "category")
    search_fields = ("text", "path")
