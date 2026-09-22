"""
Django settings for core project.

Configuration is environment-driven (12-factor). Development-friendly defaults
are used only when ``DJANGO_DEBUG`` is true; when it is false the process refuses
to start without the secrets it needs and turns on the full HTTPS/security stack.
"""

from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, []),
    DJANGO_CSRF_TRUSTED_ORIGINS=(list, []),
    CORS_ALLOWED_ORIGINS=(list, []),
    SECURE_SSL_REDIRECT=(bool, True),
    SESSION_COOKIE_SECURE=(bool, True),
    CSRF_COOKIE_SECURE=(bool, True),
    AUTH_COOKIE_SECURE=(bool, True),
    SECURE_HSTS_SECONDS=(int, 60 * 60 * 24 * 365),  # 1 year
    GITHUB_OAUTH_CLIENT_ID=(str, ""),
    GITHUB_OAUTH_CLIENT_SECRET=(str, ""),
    GITHUB_OAUTH_REDIRECT_URI=(str, "http://localhost:8000/api/auth/github/callback/"),
    GITHUB_OAUTH_SCOPE=(str, "read:user user:email"),
    AUTH_COOKIE_DOMAIN=(str, None),
    AUTH_COOKIE_SAMESITE=(str, "Strict"),
)

# Load a local .env if present (never committed).
environ.Env.read_env(BASE_DIR / ".env")

DEBUG = env("DJANGO_DEBUG")

# SECRET_KEY must be provided in production; a throwaway is generated in DEBUG.
SECRET_KEY = env("DJANGO_SECRET_KEY", default=None)
if not SECRET_KEY:
    if DEBUG:
        from django.core.management.utils import get_random_secret_key

        SECRET_KEY = get_random_secret_key()
    else:
        raise environ.ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DEBUG is false.")

ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")
if DEBUG and not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ["localhost", "127.0.0.1"]


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "accounts",
    "api",
    "reviews",
]

AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves collected static files directly from the app.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "core.wsgi.application"


# Database
# Uses DATABASE_URL when provided (e.g. postgres://…); otherwise local sqlite.

DATABASES = {
    "default": env.db_url(
        "DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
    )
}


# Password validation

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Argon2 first: stronger than the default PBKDF2 for the (rare) local passwords.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
]


# Internationalization

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# Static files

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --------------------------------------------------------------------------- #
# Django REST Framework
# --------------------------------------------------------------------------- #

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/hour",
        "user": "1000/hour",
        "auth": "20/hour",  # brute-force guard on the auth endpoints
        "reviews": "120/hour",  # review triggers are expensive (model calls)
    },
    "DEFAULT_RENDERER_CLASSES": (
        "rest_framework.renderers.JSONRenderer",
    ),
}
if DEBUG:
    # Browsable API is convenient locally; never enabled in production.
    REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] += (
        "rest_framework.renderers.BrowsableAPIRenderer",
    )


# --------------------------------------------------------------------------- #
# SimpleJWT
# --------------------------------------------------------------------------- #

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=14),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    # Refresh-token cookie hardening (consumed by accounts.services).
    "REFRESH_COOKIE_NAME": "refresh_token",
    "REFRESH_COOKIE_PATH": "/api/auth",
    "AUTH_COOKIE_SECURE": env("AUTH_COOKIE_SECURE") if not DEBUG else False,
    "AUTH_COOKIE_SAMESITE": env("AUTH_COOKIE_SAMESITE"),
    "AUTH_COOKIE_DOMAIN": env("AUTH_COOKIE_DOMAIN"),
}


# --------------------------------------------------------------------------- #
# GitHub OAuth
# --------------------------------------------------------------------------- #

GITHUB_OAUTH_CLIENT_ID = env("GITHUB_OAUTH_CLIENT_ID")
GITHUB_OAUTH_CLIENT_SECRET = env("GITHUB_OAUTH_CLIENT_SECRET")
GITHUB_OAUTH_REDIRECT_URI = env("GITHUB_OAUTH_REDIRECT_URI")
GITHUB_OAUTH_SCOPE = env("GITHUB_OAUTH_SCOPE")


# --------------------------------------------------------------------------- #
# Reviews (code-review agent)
# --------------------------------------------------------------------------- #

# LLM backend: "anthropic" (default), "openai" (OpenAI-compatible incl. Azure
# OpenAI), or "deterministic" (no model call; every review degrades cleanly).
PRCHECK_LLM_BACKEND = env("PRCHECK_LLM_BACKEND", default="anthropic")

# Anthropic
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")
ANTHROPIC_MODEL = env("ANTHROPIC_MODEL", default="claude-sonnet-4-5")
ANTHROPIC_BASE_URL = env("ANTHROPIC_BASE_URL", default="https://api.anthropic.com")

# OpenAI-compatible (set PRCHECK_OPENAI_API_VERSION to target Azure OpenAI)
PRCHECK_OPENAI_API_KEY = env("PRCHECK_OPENAI_API_KEY", default="")
PRCHECK_OPENAI_BASE_URL = env("PRCHECK_OPENAI_BASE_URL", default="https://api.openai.com/v1")
PRCHECK_OPENAI_MODEL = env("PRCHECK_OPENAI_MODEL", default="gpt-4o")
PRCHECK_OPENAI_API_VERSION = env("PRCHECK_OPENAI_API_VERSION", default="")

# Model-call tuning
PRCHECK_LLM_MAX_TOKENS = env.int("PRCHECK_LLM_MAX_TOKENS", default=4096)
PRCHECK_LLM_TEMPERATURE = env.float("PRCHECK_LLM_TEMPERATURE", default=0.0)
PRCHECK_LLM_TIMEOUT_S = env.int("PRCHECK_LLM_TIMEOUT_S", default=120)
PRCHECK_LLM_MAX_RETRIES = env.int("PRCHECK_LLM_MAX_RETRIES", default=4)

# Review behaviour
PRCHECK_ENABLE_ADVERSARY = env.bool("PRCHECK_ENABLE_ADVERSARY", default=True)
PRCHECK_REVIEW_MAX_WORKERS = env.int("PRCHECK_REVIEW_MAX_WORKERS", default=4)
# Show a GitHub check run ("prcheck / review") that goes in-progress -> pass/fail.
PRCHECK_ENABLE_CHECKS = env.bool("PRCHECK_ENABLE_CHECKS", default=True)
# PR comment that triggers a review, e.g. "/prcheck" or "/prcheck review".
PRCHECK_COMMAND_PREFIX = env("PRCHECK_COMMAND_PREFIX", default="/prcheck")

# GitHub access for fetching PRs and (optionally) publishing results.
PRCHECK_GITHUB_TOKEN = env("PRCHECK_GITHUB_TOKEN", default="")
PRCHECK_GITHUB_API_URL = env("PRCHECK_GITHUB_API_URL", default="https://api.github.com")
PRCHECK_GITHUB_WEBHOOK_SECRET = env("PRCHECK_GITHUB_WEBHOOK_SECRET", default="")

# GitHub App: the reviewer posts as the app when these are set (preferred over a
# PAT). The private key is a multi-line PEM, so it can arrive base64-encoded
# (prod, avoids newline issues in env vars) or as a file path (local dev).
PRCHECK_GITHUB_APP_ID = env("PRCHECK_GITHUB_APP_ID", default="")
_app_key_b64 = env("PRCHECK_GITHUB_APP_PRIVATE_KEY_B64", default="")
_app_key_path = env("PRCHECK_GITHUB_APP_PRIVATE_KEY_PATH", default="")
if _app_key_b64:
    import base64
    PRCHECK_GITHUB_APP_PRIVATE_KEY = base64.b64decode(_app_key_b64).decode("utf-8")
elif _app_key_path and Path(_app_key_path).exists():
    PRCHECK_GITHUB_APP_PRIVATE_KEY = Path(_app_key_path).read_text(encoding="utf-8")
else:
    PRCHECK_GITHUB_APP_PRIVATE_KEY = env("PRCHECK_GITHUB_APP_PRIVATE_KEY", default="")

# Publishing findings back to the PR (sticky summary + inline comments).
PRCHECK_PUBLISH_REVIEWS = env.bool("PRCHECK_PUBLISH_REVIEWS", default=False)
PRCHECK_MAX_INLINE_COMMENTS = env.int("PRCHECK_MAX_INLINE_COMMENTS", default=40)


# --------------------------------------------------------------------------- #
# CORS / CSRF
# --------------------------------------------------------------------------- #

CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
if DEBUG and not CORS_ALLOWED_ORIGINS:
    CORS_ALLOWED_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
CORS_ALLOW_CREDENTIALS = True

CSRF_TRUSTED_ORIGINS = env("DJANGO_CSRF_TRUSTED_ORIGINS")


# --------------------------------------------------------------------------- #
# Security hardening (active whenever DEBUG is false)
# --------------------------------------------------------------------------- #

SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

if not DEBUG:
    SECURE_SSL_REDIRECT = env("SECURE_SSL_REDIRECT")
    SESSION_COOKIE_SECURE = env("SESSION_COOKIE_SECURE")
    CSRF_COOKIE_SECURE = env("CSRF_COOKIE_SECURE")
    # Trust the X-Forwarded-Proto header set by a TLS-terminating proxy.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = env("SECURE_HSTS_SECONDS")
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "accounts": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "reviews": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
