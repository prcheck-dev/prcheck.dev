"""Tests for the GitHub-OAuth-only authentication system.

GitHub network calls are mocked so the whole flow runs offline.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from . import github

User = get_user_model()

OAUTH_SETTINGS = dict(
    GITHUB_OAUTH_CLIENT_ID="test-client-id",
    GITHUB_OAUTH_CLIENT_SECRET="test-secret",
    GITHUB_OAUTH_REDIRECT_URI="http://testserver/api/auth/github/callback/",
    GITHUB_OAUTH_SCOPE="read:user user:email",
)

GH_PROFILE = {
    "id": 4242,
    "login": "octocat",
    "name": "The Octocat",
    "email": None,  # hidden -> must fall back to /user/emails
    "avatar_url": "https://avatars.githubusercontent.com/u/4242",
}


class PublicEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_health_is_public(self):
        resp = self.client.get("/api/health/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")

    def test_me_requires_authentication(self):
        resp = self.client.get(reverse("accounts:me"))
        self.assertEqual(resp.status_code, 401)


class GithubLoginTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_login_returns_503_when_unconfigured(self):
        with override_settings(GITHUB_OAUTH_CLIENT_ID=""):
            resp = self.client.get(reverse("accounts:github-login"))
        self.assertEqual(resp.status_code, 503)

    @override_settings(**OAUTH_SETTINGS)
    def test_login_redirects_to_github_with_state_cookie(self):
        resp = self.client.get(reverse("accounts:github-login"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("github.com/login/oauth/authorize", resp["Location"])
        self.assertIn("gh_oauth_state", resp.cookies)
        state_cookie = resp.cookies["gh_oauth_state"]
        self.assertTrue(state_cookie["httponly"])
        self.assertIn(f"state={state_cookie.value}", resp["Location"])


@override_settings(**OAUTH_SETTINGS)
class GithubCallbackTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _begin_flow(self):
        """Run /login to obtain a valid state and prime the state cookie."""
        resp = self.client.get(reverse("accounts:github-login"))
        return resp.cookies["gh_oauth_state"].value

    def test_callback_rejects_missing_state(self):
        resp = self.client.get(reverse("accounts:github-callback"), {"code": "abc"})
        self.assertEqual(resp.status_code, 400)

    def test_callback_rejects_forged_state(self):
        self._begin_flow()  # sets the real state cookie on the client
        resp = self.client.get(
            reverse("accounts:github-callback"), {"code": "abc", "state": "not-the-real-state"}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"], "Invalid OAuth state.")

    def test_callback_reports_github_denial(self):
        resp = self.client.get(
            reverse("accounts:github-callback"),
            {"error": "access_denied", "error_description": "The user denied access."},
        )
        self.assertEqual(resp.status_code, 400)

    @mock.patch.object(github, "fetch_primary_email", return_value="octocat@example.com")
    @mock.patch.object(github, "fetch_user", return_value=GH_PROFILE)
    @mock.patch.object(github, "exchange_code_for_token", return_value="gh-access-token")
    def test_callback_happy_path_creates_user_and_issues_tokens(self, m_exchange, m_user, m_email):
        state = self._begin_flow()
        resp = self.client.get(
            reverse("accounts:github-callback"), {"code": "valid-code", "state": state}
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("access_token", body)
        self.assertEqual(body["token_type"], "Bearer")
        self.assertEqual(body["user"]["email"], "octocat@example.com")
        self.assertEqual(body["user"]["github_login"], "octocat")

        # Refresh token delivered only as an httpOnly cookie.
        self.assertIn("refresh_token", resp.cookies)
        self.assertTrue(resp.cookies["refresh_token"]["httponly"])

        user = User.objects.get(github_id=4242)
        self.assertFalse(user.has_usable_password())  # OAuth-only account

    @mock.patch.object(github, "fetch_primary_email", return_value="octocat@example.com")
    @mock.patch.object(github, "fetch_user", return_value=GH_PROFILE)
    @mock.patch.object(github, "exchange_code_for_token", return_value="gh-access-token")
    def test_second_login_updates_not_duplicates(self, *_):
        for _ in range(2):
            state = self._begin_flow()
            self.client.get(
                reverse("accounts:github-callback"), {"code": "valid-code", "state": state}
            )
        self.assertEqual(User.objects.filter(github_id=4242).count(), 1)


@override_settings(**OAUTH_SETTINGS)
class SessionLifecycleTests(TestCase):
    """End-to-end: login -> me -> refresh -> logout."""

    def setUp(self):
        self.client = APIClient()

    @mock.patch.object(github, "fetch_primary_email", return_value="octocat@example.com")
    @mock.patch.object(github, "fetch_user", return_value=GH_PROFILE)
    @mock.patch.object(github, "exchange_code_for_token", return_value="gh-access-token")
    def _authenticate(self, *_):
        state = self.client.get(reverse("accounts:github-login")).cookies["gh_oauth_state"].value
        resp = self.client.get(
            reverse("accounts:github-callback"), {"code": "valid-code", "state": state}
        )
        return resp.json()["access_token"]

    def test_full_lifecycle(self):
        access = self._authenticate()

        # /me with the access token
        resp = self.client.get(
            reverse("accounts:me"), HTTP_AUTHORIZATION=f"Bearer {access}"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["github_login"], "octocat")

        # refresh (refresh cookie already on the client)
        resp = self.client.post(reverse("accounts:token-refresh"))
        self.assertEqual(resp.status_code, 200)
        new_access = resp.json()["access_token"]
        self.assertTrue(new_access)

        # logout blacklists the refresh token
        resp = self.client.post(
            reverse("accounts:logout"), HTTP_AUTHORIZATION=f"Bearer {new_access}"
        )
        self.assertEqual(resp.status_code, 205)

        # refresh after logout must fail (rotated + blacklisted)
        resp = self.client.post(reverse("accounts:token-refresh"))
        self.assertEqual(resp.status_code, 401)

    def test_refresh_without_cookie_is_unauthorized(self):
        resp = self.client.post(reverse("accounts:token-refresh"))
        self.assertEqual(resp.status_code, 401)

    def test_rotated_refresh_token_is_blacklisted(self):
        self._authenticate()
        first = self.client.cookies["refresh_token"].value

        # First refresh rotates the cookie.
        self.client.post(reverse("accounts:token-refresh"))

        # Replaying the ORIGINAL refresh token must be rejected.
        self.client.cookies["refresh_token"] = first
        resp = self.client.post(reverse("accounts:token-refresh"))
        self.assertEqual(resp.status_code, 401)
