from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("email", "username", "github_login", "is_staff", "is_active", "date_joined")
    search_fields = ("email", "username", "github_login", "github_id")
    ordering = ("-date_joined",)
    readonly_fields = ("github_id", "github_login", "avatar_url", "created_at", "updated_at", "last_login", "date_joined")

    fieldsets = DjangoUserAdmin.fieldsets + (
        ("GitHub", {"fields": ("github_id", "github_login", "avatar_url")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )
