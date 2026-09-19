"""JWT authentication helpers.

Access tokens are sent by clients in the ``Authorization: Bearer <token>``
header (handled by SimpleJWT's default ``JWTAuthentication``). The long-lived
refresh token never travels in a header or response body — it lives only in a
hardened, httpOnly cookie and is read back at the refresh endpoint via
:func:`get_refresh_token_from_cookie`.
"""
from django.conf import settings
from rest_framework.request import Request


def get_refresh_token_from_cookie(request: Request) -> str | None:
    return request.COOKIES.get(settings.SIMPLE_JWT["REFRESH_COOKIE_NAME"])
