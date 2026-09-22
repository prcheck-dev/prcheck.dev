"""GitHub App authentication.

The reviewer acts as the installed GitHub App: it signs a short-lived JWT with
the app's private key, resolves the installation for a repo, and exchanges it for
an installation access token (valid ~1h) used to read PRs and post comments as
the app identity. Tokens are cached per repo until shortly before they expire.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import jwt
from django.conf import settings

from .github_client import GitHubAPI, GitHubAPIError

# repo -> (token, expiry_epoch)
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_LOCK = threading.Lock()


def _conf(name: str, default=""):
    return getattr(settings, name, default) or default


def is_configured() -> bool:
    return bool(_conf("PRCHECK_GITHUB_APP_ID") and _conf("PRCHECK_GITHUB_APP_PRIVATE_KEY"))


def _app_jwt() -> str:
    now = int(time.time())
    # 60s backdate absorbs clock skew; GitHub caps app JWT lifetime at 10 min.
    payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": str(_conf("PRCHECK_GITHUB_APP_ID"))}
    return jwt.encode(payload, _conf("PRCHECK_GITHUB_APP_PRIVATE_KEY"), algorithm="RS256")


def _base() -> str:
    return _conf("PRCHECK_GITHUB_API_URL", "https://api.github.com")


def _installation_id(repo: str) -> int:
    api = GitHubAPI(repo=repo, token=_app_jwt(), base=_base())
    result = api.get(f"/repos/{repo}/installation")
    inst_id = result.get("id") if isinstance(result, dict) else None
    if not inst_id:
        raise GitHubAPIError(f"GitHub App is not installed on {repo}")
    return int(inst_id)


def token_for_repo(repo: str) -> str:
    """Return a valid installation token for the repo, minting/caching as needed."""
    with _LOCK:
        cached = _TOKEN_CACHE.get(repo)
        if cached and cached[1] > time.time() + 60:
            return cached[0]

    inst_id = _installation_id(repo)
    api = GitHubAPI(repo=repo, token=_app_jwt(), base=_base())
    result = api.post(f"/app/installations/{inst_id}/access_tokens", {})
    token = result.get("token") if isinstance(result, dict) else None
    if not token:
        raise GitHubAPIError(f"Could not mint an installation token for {repo}")
    expiry = _parse_expiry(result.get("expires_at"))

    with _LOCK:
        _TOKEN_CACHE[repo] = (token, expiry)
    return token


def _parse_expiry(value) -> float:
    if not value:
        return time.time() + 50 * 60
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time() + 50 * 60
