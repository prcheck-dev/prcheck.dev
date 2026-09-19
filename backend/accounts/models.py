"""Custom user model backed by GitHub OAuth.

Accounts are provisioned exclusively through the GitHub OAuth flow, so users
have no usable local password. We still inherit from ``AbstractUser`` to keep
Django's permission / group machinery and admin integration intact, and to
allow operators to bootstrap a superuser via ``createsuperuser``.
"""
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Application user.

    The GitHub numeric id is the immutable link to the upstream account;
    ``github_login`` (the handle) can change over time, so it is stored for
    display but never used as the join key.
    """

    email = models.EmailField("email address", unique=True)

    github_id = models.BigIntegerField(unique=True, null=True, blank=True, db_index=True)
    github_login = models.CharField(max_length=255, blank=True)
    avatar_url = models.URLField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.email or self.username
