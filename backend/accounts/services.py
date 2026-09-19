"""Domain services: provisioning users from GitHub and issuing token cookies."""
from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import RefreshToken

from . import github

User = get_user_model()


def upsert_user_from_github(token: str) -> User:
    """Create or update the local user for a verified GitHub account.

    Matching is done on the immutable numeric GitHub id. The handle, name,
    avatar and email are refreshed on every login so local data does not drift.
    """
    profile = github.fetch_user(token)
    github_id = profile.get("id")
    if not github_id:
        raise github.GitHubOAuthError("GitHub profile is missing an id")

    email = profile.get("email") or github.fetch_primary_email(token)
    if not email:
        raise github.GitHubOAuthError(
            "No verified email available from GitHub. Grant the user:email scope "
            "or verify an email on GitHub."
        )

    login = profile.get("login", "")
    defaults = {
        "email": email,
        "github_login": login,
        "avatar_url": profile.get("avatar_url", "") or "",
        "first_name": (profile.get("name") or "")[:150],
    }

    user, created = User.objects.get_or_create(
        github_id=github_id,
        defaults={"username": _unique_username(login, github_id), **defaults},
    )
    if created:
        # OAuth-only accounts never have a usable local password.
        user.set_unusable_password()
        user.save(update_fields=["password"])
    else:
        changed = False
        for field, value in defaults.items():
            if getattr(user, field) != value:
                setattr(user, field, value)
                changed = True
        if changed:
            user.save(update_fields=list(defaults.keys()))

    return user


def _unique_username(login: str, github_id: int) -> str:
    """Derive a stable, collision-free username from the GitHub handle."""
    base = (login or f"gh{github_id}")[:150]
    candidate = base
    suffix = 0
    while User.objects.filter(username=candidate).exists():
        suffix += 1
        tail = f"-{suffix}"
        candidate = f"{base[: 150 - len(tail)]}{tail}"
    return candidate


def issue_tokens(user: User) -> tuple[str, RefreshToken]:
    """Return a fresh (access_token_string, refresh_token) pair for the user."""
    refresh = RefreshToken.for_user(user)
    return str(refresh.access_token), refresh


def set_refresh_cookie(response, refresh_token: str) -> None:
    conf = settings.SIMPLE_JWT
    response.set_cookie(
        key=conf["REFRESH_COOKIE_NAME"],
        value=refresh_token,
        max_age=int(conf["REFRESH_TOKEN_LIFETIME"].total_seconds()),
        httponly=True,
        secure=conf["AUTH_COOKIE_SECURE"],
        samesite=conf["AUTH_COOKIE_SAMESITE"],
        path=conf["REFRESH_COOKIE_PATH"],
        domain=conf["AUTH_COOKIE_DOMAIN"],
    )


def clear_refresh_cookie(response) -> None:
    conf = settings.SIMPLE_JWT
    response.delete_cookie(
        key=conf["REFRESH_COOKIE_NAME"],
        path=conf["REFRESH_COOKIE_PATH"],
        domain=conf["AUTH_COOKIE_DOMAIN"],
        samesite=conf["AUTH_COOKIE_SAMESITE"],
    )
