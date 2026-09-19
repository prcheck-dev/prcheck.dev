"""Authentication endpoints (GitHub OAuth only).

Flow
----
1. ``GET  /api/auth/github/login/``     -> 302 to GitHub, sets a signed state cookie
2. ``GET  /api/auth/github/callback/``  -> validates state, exchanges code, issues tokens
3. ``POST /api/auth/token/refresh/``    -> rotates the refresh cookie, returns a new access token
4. ``POST /api/auth/logout/``           -> blacklists the refresh token, clears the cookie
5. ``GET  /api/auth/me/``               -> returns the authenticated user

Access tokens are returned in the JSON body (clients send them back in the
``Authorization`` header). The refresh token is only ever stored in a hardened
httpOnly cookie, so it is invisible to JavaScript and immune to header theft.
"""
from __future__ import annotations

import secrets

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
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from . import github, services
from .authentication import get_refresh_token_from_cookie
from .serializers import UserSerializer

STATE_COOKIE_NAME = "gh_oauth_state"
STATE_COOKIE_MAX_AGE = 600  # 10 minutes to complete the round trip


class AuthRateThrottle(ScopedRateThrottle):
    scope = "auth"


def _access_response(user, access_token, refresh, http_status=status.HTTP_200_OK):
    response = Response(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": int(settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds()),
            "user": UserSerializer(user).data,
        },
        status=http_status,
    )
    services.set_refresh_cookie(response, str(refresh))
    return response


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([AuthRateThrottle])
def github_login(request):
    """Kick off the OAuth dance by redirecting the browser to GitHub."""
    if not settings.GITHUB_OAUTH_CLIENT_ID:
        return Response(
            {"detail": "GitHub OAuth is not configured on the server."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    state = secrets.token_urlsafe(32)
    authorize_url = github.build_authorize_url(state)

    response = Response(status=status.HTTP_302_FOUND)
    response["Location"] = authorize_url
    # State cookie must survive GitHub's top-level GET redirect back to us, so
    # SameSite=Lax (not Strict). Signed + httpOnly to block tampering/theft.
    response.set_cookie(
        key=STATE_COOKIE_NAME,
        value=state,
        max_age=STATE_COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.SIMPLE_JWT["AUTH_COOKIE_SECURE"],
        samesite="Lax",
        path="/api/auth/github/callback/",
    )
    return response


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([AuthRateThrottle])
def github_callback(request):
    """Handle GitHub's redirect: validate state, exchange code, issue tokens."""
    # GitHub reports user-facing errors (e.g. access_denied) as query params.
    if "error" in request.query_params:
        return Response(
            {"detail": request.query_params.get("error_description", "Authorization denied.")},
            status=status.HTTP_400_BAD_REQUEST,
        )

    code = request.query_params.get("code")
    returned_state = request.query_params.get("state")
    expected_state = request.COOKIES.get(STATE_COOKIE_NAME)

    if not code or not returned_state:
        return Response({"detail": "Missing code or state."}, status=status.HTTP_400_BAD_REQUEST)

    # CSRF defense for the OAuth flow: the state must match what we set, and be
    # compared in constant time.
    if not expected_state or not secrets.compare_digest(returned_state, expected_state):
        return Response({"detail": "Invalid OAuth state."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        token = github.exchange_code_for_token(code)
        user = services.upsert_user_from_github(token)
    except github.GitHubOAuthError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    if not user.is_active:
        return Response({"detail": "This account is disabled."}, status=status.HTTP_403_FORBIDDEN)

    access_token, refresh = services.issue_tokens(user)
    response = _access_response(user, access_token, refresh)
    # State cookie is single-use; drop it once consumed.
    response.delete_cookie(STATE_COOKIE_NAME, path="/api/auth/github/callback/")
    return response


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([AuthRateThrottle])
def token_refresh(request):
    """Issue a new access token and rotate the refresh cookie."""
    raw_refresh = get_refresh_token_from_cookie(request)
    if not raw_refresh:
        return Response({"detail": "No refresh token."}, status=status.HTTP_401_UNAUTHORIZED)

    try:
        refresh = RefreshToken(raw_refresh)
    except TokenError:
        response = Response({"detail": "Invalid or expired refresh token."}, status=status.HTTP_401_UNAUTHORIZED)
        services.clear_refresh_cookie(response)
        return response

    access_token = str(refresh.access_token)

    # Rotation + blacklist: the old refresh token is invalidated and a new one
    # is minted, so a stolen refresh token has a bounded blast radius.
    if settings.SIMPLE_JWT.get("ROTATE_REFRESH_TOKENS"):
        try:
            refresh.blacklist()
        except AttributeError:
            pass  # blacklist app not installed
        user_id = refresh.get(settings.SIMPLE_JWT["USER_ID_CLAIM"])
        from django.contrib.auth import get_user_model

        try:
            user = get_user_model().objects.get(pk=user_id)
        except get_user_model().DoesNotExist:
            return Response({"detail": "User no longer exists."}, status=status.HTTP_401_UNAUTHORIZED)
        new_refresh = RefreshToken.for_user(user)
        access_token = str(new_refresh.access_token)
        refresh = new_refresh

    response = Response(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": int(settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds()),
        }
    )
    services.set_refresh_cookie(response, str(refresh))
    return response


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def logout(request):
    """Blacklist the current refresh token and clear its cookie."""
    raw_refresh = get_refresh_token_from_cookie(request)
    if raw_refresh:
        try:
            RefreshToken(raw_refresh).blacklist()
        except (TokenError, AttributeError):
            pass

    response = Response(status=status.HTTP_205_RESET_CONTENT)
    services.clear_refresh_cookie(response)
    return response


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    """Return the authenticated user's profile."""
    return Response(UserSerializer(request.user).data)
