"""Thin, defensive client for the GitHub OAuth web application flow.

Only the pieces we need: build the authorize URL, exchange the authorization
code for an access token, and read the authenticated user's profile + primary
verified email. Every outbound call is time-bounded and every failure is
surfaced as :class:`GitHubOAuthError` so views can translate it into a clean
4xx/5xx instead of leaking a traceback.
"""
from __future__ import annotations

from urllib.parse import urlencode

import requests
from django.conf import settings

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_API_URL = "https://api.github.com/user"
USER_EMAILS_API_URL = "https://api.github.com/user/emails"

# GitHub can be slow; keep a tight but not hair-trigger timeout.
_TIMEOUT = 10


class GitHubOAuthError(Exception):
    """Raised for any failure while talking to GitHub."""


def build_authorize_url(state: str) -> str:
    """Return the GitHub URL the user's browser should be redirected to."""
    params = {
        "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
        "redirect_uri": settings.GITHUB_OAUTH_REDIRECT_URI,
        "scope": settings.GITHUB_OAUTH_SCOPE,
        "state": state,
        "allow_signup": "true",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code_for_token(code: str) -> str:
    """Exchange a one-time authorization code for a user access token."""
    try:
        resp = requests.post(
            ACCESS_TOKEN_URL,
            data={
                "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
                "client_secret": settings.GITHUB_OAUTH_CLIENT_SECRET,
                "code": code,
                "redirect_uri": settings.GITHUB_OAUTH_REDIRECT_URI,
            },
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:  # network/timeout
        raise GitHubOAuthError(f"Could not reach GitHub: {exc}") from exc

    if resp.status_code != 200:
        raise GitHubOAuthError(f"Token exchange failed ({resp.status_code})")

    payload = resp.json()
    if "error" in payload:
        # e.g. bad_verification_code, redirect_uri_mismatch
        raise GitHubOAuthError(payload.get("error_description", payload["error"]))

    token = payload.get("access_token")
    if not token:
        raise GitHubOAuthError("GitHub did not return an access token")
    return token


def _auth_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def fetch_user(token: str) -> dict:
    """Return the authenticated user's GitHub profile."""
    try:
        resp = requests.get(USER_API_URL, headers=_auth_headers(token), timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise GitHubOAuthError(f"Could not reach GitHub: {exc}") from exc

    if resp.status_code != 200:
        raise GitHubOAuthError(f"Failed to load GitHub profile ({resp.status_code})")
    return resp.json()


def fetch_primary_email(token: str) -> str | None:
    """Return the user's primary, verified email address if available.

    The ``/user`` endpoint may return a null email when the user hides it, so we
    consult ``/user/emails`` (requires the ``user:email`` scope) and only accept
    a verified address.
    """
    try:
        resp = requests.get(USER_EMAILS_API_URL, headers=_auth_headers(token), timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise GitHubOAuthError(f"Could not reach GitHub: {exc}") from exc

    if resp.status_code != 200:
        return None

    emails = resp.json()
    if not isinstance(emails, list):
        return None

    primary = next(
        (e for e in emails if e.get("primary") and e.get("verified")), None
    )
    if primary:
        return primary["email"]

    # Fall back to any verified address.
    verified = next((e for e in emails if e.get("verified")), None)
    return verified["email"] if verified else None
