"""Tests for the code-review agent. All model/GitHub calls are faked (offline)."""
from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from . import github_client as gh
from . import services
from .adversary import run_adversary
from .budget import Budget
from .llm import Completer, _adapt_reasoning_payload, structured_call
from .models import Review
from .reviewer import build_review_shards, merge_review_results
from .schema import REVIEWER_SCHEMA, SchemaError, validate
from .verdict import APPROVE, APPROVE_COND, BLOCK, REQUEST_CHANGES, compute_verdict

User = get_user_model()

REVIEW_SETTINGS = dict(
    PRCHECK_GITHUB_TOKEN="test-token",
    ANTHROPIC_API_KEY="test-key",
    PRCHECK_LLM_BACKEND="anthropic",
    PRCHECK_PUBLISH_REVIEWS=False,
)


def _snapshot(diff_text="@@ -1,2 +1,3 @@\n+bad line\n", files=None):
    return gh.PullSnapshot(
        title="Fix things",
        url="https://github.com/o/r/pull/7",
        head_sha="abc123",
        files=files or [{"filename": "app/db.py", "status": "modified",
                         "additions": 1, "deletions": 0, "patch": diff_text}],
        diff_text="diff --git a/app/db.py b/app/db.py\n--- a/app/db.py\n+++ b/app/db.py\n" + diff_text,
        additions=1,
        deletions=0,
    )


def _fake_complete(findings=None, adversary_verdict="CONCERNS"):
    """Return a Completer.complete stand-in that answers per system prompt."""
    findings = findings if findings is not None else [
        {"text": "SQL built via string interpolation", "path": "app/db.py",
         "line": 2, "severity": "critical", "category": "security"},
    ]

    def _complete(self, system_prompt, prompt, *, max_tokens=None):
        if "Adversarial Verifier" in system_prompt:
            text = json.dumps({"verdict": adversary_verdict, "findings": []})
        else:
            text = json.dumps({"findings": findings})
        self.last_usage = {"provider": "fake", "total_tokens": 20,
                           "prompt_tokens": 10, "completion_tokens": 10, "usd": 0.0}
        return text, self.last_usage

    return _complete


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
class SchemaTests(TestCase):
    def test_valid_output_passes(self):
        validate({"findings": [{"text": "x", "path": "a.py", "line": 1,
                                "severity": "high", "category": "bug"}]})

    def test_bad_severity_rejected(self):
        with self.assertRaises(SchemaError):
            validate({"findings": [{"text": "x", "path": "a.py", "line": 1,
                                    "severity": "nope", "category": "bug"}]})

    def test_missing_findings_rejected(self):
        with self.assertRaises(SchemaError):
            validate({})


# --------------------------------------------------------------------------- #
# Sharding + merge
# --------------------------------------------------------------------------- #
class ShardingTests(TestCase):
    def test_small_pr_is_single_shard(self):
        shards = build_review_shards([{"path": "a.py"}], "diff --git a/a.py b/a.py\n+x\n")
        self.assertEqual(len(shards), 1)

    @override_settings(PRCHECK_REVIEW_PARALLEL_THRESHOLD_LINES=5, PRCHECK_REVIEW_MAX_SHARDS=3)
    def test_large_multi_file_pr_splits(self):
        diff = "".join(
            f"diff --git a/f{i}.py b/f{i}.py\n@@ -1 +1,3 @@\n+line{i}a\n+line{i}b\n+line{i}c\n"
            for i in range(6)
        )
        files = [{"path": f"f{i}.py"} for i in range(6)]
        shards = build_review_shards(files, diff)
        self.assertGreater(len(shards), 1)
        # Every file is represented exactly once across shards.
        seen = [cf["path"] for s in shards for cf in s.changed_files]
        self.assertEqual(sorted(seen), sorted(f["path"] for f in files))

    def test_merge_dedupes_and_orders_by_severity(self):
        r1 = {"findings": [
            {"text": "dup", "path": "a.py", "line": 1, "severity": "low", "category": "bug"},
            {"text": "crit", "path": "b.py", "line": 2, "severity": "critical", "category": "security"},
        ]}
        r2 = {"findings": [
            {"text": "DUP", "path": "a.py", "line": 1, "severity": "low", "category": "bug"},  # dup (casefold)
            {"text": "warn", "path": "c.py", "line": 3, "severity": "medium", "category": "perf"},
        ]}
        merged = merge_review_results([r1, r2])
        sevs = [f["severity"] for f in merged["findings"]]
        self.assertEqual(sevs, ["critical", "medium", "low"])  # deduped + ordered
        self.assertEqual(merged["_review_shards"], 2)

    def test_merge_all_none_is_none(self):
        self.assertIsNone(merge_review_results([None, None]))


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #
class VerdictTests(TestCase):
    def test_critical_blocks(self):
        v, _ = compute_verdict([{"severity": "critical"}])
        self.assertEqual(v, BLOCK)

    def test_high_requests_changes(self):
        v, _ = compute_verdict([{"severity": "high"}])
        self.assertEqual(v, REQUEST_CHANGES)

    def test_medium_only_is_conditional(self):
        v, cond = compute_verdict([{"severity": "medium"}, {"severity": "low"}])
        self.assertEqual(v, APPROVE_COND)
        self.assertTrue(cond)

    def test_clean_approves(self):
        v, cond = compute_verdict([])
        self.assertEqual(v, APPROVE)
        self.assertEqual(cond, [])

    def test_degraded_caps_to_conditional(self):
        v, _ = compute_verdict([], degraded=True)
        self.assertEqual(v, APPROVE_COND)


# --------------------------------------------------------------------------- #
# Diff parsing
# --------------------------------------------------------------------------- #
class DiffTests(TestCase):
    def test_added_lines_parsed(self):
        diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
                "@@ -1,2 +1,3 @@\n context\n+added1\n+added2\n")
        added = gh.added_lines_by_file(diff)
        self.assertEqual(added["x.py"], {2, 3})

    def test_unified_diff_rebuilt_from_files(self):
        files = [{"filename": "n.py", "status": "added", "patch": "@@ -0,0 +1 @@\n+new"}]
        diff = gh.unified_diff_from_pull_files(files)
        self.assertIn("diff --git a/n.py b/n.py", diff)
        self.assertIn("--- /dev/null", diff)


# --------------------------------------------------------------------------- #
# Adversary
# --------------------------------------------------------------------------- #
class AdversaryTests(TestCase):
    @override_settings(**REVIEW_SETTINGS)
    def test_uncited_block_downgraded_to_concerns(self):
        def _complete(self, system_prompt, prompt, *, max_tokens=None):
            self.last_usage = {"provider": "fake", "total_tokens": 5}
            # BLOCKER with a citation to a file NOT in the diff -> unverified.
            return json.dumps({"verdict": "BLOCK", "findings": [
                {"persona": "saboteur", "severity": "BLOCKER",
                 "text": "made up", "citation": "other/file.py:9"}]}), self.last_usage

        with mock.patch.object(Completer, "complete", _complete):
            result = run_adversary(Completer(), Budget(),
                                   diff_text="+++ b/app/db.py\n+x\n", review=None)
        self.assertEqual(result["verdict"], "CONCERNS")  # downgraded
        self.assertTrue(result["findings"][0]["unverified"])


# --------------------------------------------------------------------------- #
# LLM choke point
# --------------------------------------------------------------------------- #
class LLMTests(TestCase):
    @override_settings(PRCHECK_LLM_BACKEND="deterministic")
    def test_deterministic_backend_degrades(self):
        result = structured_call(Completer(), Budget(), session="reviewer",
                                 system_prompt="s", prompt="p", schema=REVIEWER_SCHEMA)
        self.assertIsNone(result)

    def test_reasoning_payload_adaptation(self):
        base = {"model": "gpt-5.2", "temperature": 0, "max_tokens": 4096,
                "response_format": {"type": "json_object"}}
        adapted = _adapt_reasoning_payload(
            base, "openai HTTP 400: Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens'.")
        self.assertIn("max_completion_tokens", adapted)
        self.assertNotIn("max_tokens", adapted)
        # An unrelated 400 is not masked by a retry.
        self.assertIsNone(_adapt_reasoning_payload(base, "openai HTTP 400: bad content filter"))

    @override_settings(**REVIEW_SETTINGS)
    def test_retry_on_invalid_then_success(self):
        calls = {"n": 0}

        def _complete(self, system_prompt, prompt, *, max_tokens=None):
            self.last_usage = {"provider": "fake", "total_tokens": 3}
            calls["n"] += 1
            if calls["n"] == 1:
                return "not json", self.last_usage
            return json.dumps({"findings": []}), self.last_usage

        with mock.patch.object(Completer, "complete", _complete):
            result = structured_call(Completer(), Budget(), session="reviewer",
                                     system_prompt="s", prompt="p", schema=REVIEWER_SCHEMA)
        self.assertEqual(result, {"findings": []})
        self.assertEqual(calls["n"], 2)  # retried exactly once


# --------------------------------------------------------------------------- #
# End-to-end orchestration
# --------------------------------------------------------------------------- #
@override_settings(**REVIEW_SETTINGS)
class RunReviewTests(TestCase):
    def test_run_review_persists_findings_and_verdict(self):
        review = Review.objects.create(repo="o/r", pr_number=7, trigger="cli")
        with mock.patch.object(services.gh, "fetch_pull_snapshot", return_value=_snapshot()), \
             mock.patch.object(Completer, "complete", _fake_complete()):
            result = services.run_review(review.pk)
        self.assertEqual(result.status, Review.Status.COMPLETED)
        self.assertEqual(result.verdict, BLOCK)  # critical finding
        self.assertEqual(result.findings.count(), 1)
        self.assertFalse(result.degraded)

    def test_missing_token_fails_cleanly(self):
        review = Review.objects.create(repo="o/r", pr_number=7)
        with override_settings(PRCHECK_GITHUB_TOKEN=""):
            result = services.run_review(review.pk)
        self.assertEqual(result.status, Review.Status.FAILED)
        self.assertIn("PRCHECK_GITHUB_TOKEN", result.error)

    @override_settings(PRCHECK_LLM_BACKEND="deterministic")
    def test_degraded_run_completes_with_conditional_verdict(self):
        review = Review.objects.create(repo="o/r", pr_number=7)
        with mock.patch.object(services.gh, "fetch_pull_snapshot", return_value=_snapshot()):
            result = services.run_review(review.pk)
        self.assertEqual(result.status, Review.Status.COMPLETED)
        self.assertTrue(result.degraded)
        self.assertEqual(result.verdict, APPROVE_COND)


# --------------------------------------------------------------------------- #
# GitHub App auth
# --------------------------------------------------------------------------- #
class GitHubAppTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

    def test_not_configured_by_default(self):
        from . import github_app
        with override_settings(PRCHECK_GITHUB_APP_ID="", PRCHECK_GITHUB_APP_PRIVATE_KEY=""):
            self.assertFalse(github_app.is_configured())

    def test_token_for_repo_mints_and_caches(self):
        from . import github_app
        github_app._TOKEN_CACHE.clear()
        calls = {"post": 0}

        def fake_get(self, path):
            return {"id": 999}

        def fake_post(self, path, body):
            calls["post"] += 1
            return {"token": "ghs_installtoken", "expires_at": "2999-01-01T00:00:00Z"}

        with override_settings(PRCHECK_GITHUB_APP_ID="4886852",
                               PRCHECK_GITHUB_APP_PRIVATE_KEY=self.pem), \
             mock.patch.object(gh.GitHubAPI, "get", fake_get), \
             mock.patch.object(gh.GitHubAPI, "post", fake_post):
            self.assertTrue(github_app.is_configured())
            t1 = github_app.token_for_repo("o/r")
            t2 = github_app.token_for_repo("o/r")  # served from cache

        self.assertEqual(t1, "ghs_installtoken")
        self.assertEqual(t2, "ghs_installtoken")
        self.assertEqual(calls["post"], 1)  # minted once, then cached


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
class APITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="u", email="u@e.com", password="x" * 12)

    def test_create_review_requires_auth(self):
        resp = self.client.post(reverse("reviews:reviews"), {"repo": "o/r", "pr_number": 1})
        self.assertEqual(resp.status_code, 401)

    def test_create_review_returns_202_and_schedules(self):
        self.client.force_authenticate(self.user)
        with mock.patch.object(services, "run_review_in_background") as sched, \
             mock.patch("reviews.views.run_review_in_background", sched):
            resp = self.client.post(reverse("reviews:reviews"),
                                    {"repo": "o/r", "pr_number": 42}, format="json")
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json()["repo"], "o/r")
        sched.assert_called_once()

    def test_create_review_rejects_bad_repo(self):
        self.client.force_authenticate(self.user)
        resp = self.client.post(reverse("reviews:reviews"),
                                {"repo": "not-a-repo", "pr_number": 1}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_webhook_rejects_bad_signature(self):
        with override_settings(PRCHECK_GITHUB_WEBHOOK_SECRET="s"):
            resp = self.client.post(
                reverse("reviews:github-webhook"),
                data=json.dumps({"action": "opened"}), content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256="sha256=deadbeef", HTTP_X_GITHUB_EVENT="pull_request",
            )
        self.assertEqual(resp.status_code, 401)

    def test_webhook_unconfigured_returns_503(self):
        with override_settings(PRCHECK_GITHUB_WEBHOOK_SECRET=""):
            resp = self.client.post(reverse("reviews:github-webhook"),
                                    data="{}", content_type="application/json")
        self.assertEqual(resp.status_code, 503)

    def _signed(self, event, payload, secret="s"):
        import hashlib
        import hmac
        body = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post(
            reverse("reviews:github-webhook"), data=body, content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=sig, HTTP_X_GITHUB_EVENT=event,
        )

    @override_settings(PRCHECK_GITHUB_WEBHOOK_SECRET="s")
    def test_comment_command_triggers_review(self):
        payload = {
            "action": "created",
            "issue": {"number": 7, "pull_request": {"url": "x"}},
            "comment": {"body": "/prcheck review"},
            "repository": {"full_name": "o/r"},
        }
        with mock.patch("reviews.views.run_review_in_background") as sched:
            resp = self._signed("issue_comment", payload)
        self.assertEqual(resp.status_code, 202)
        self.assertTrue(Review.objects.filter(repo="o/r", pr_number=7, trigger="command").exists())
        sched.assert_called_once()

    @override_settings(PRCHECK_GITHUB_WEBHOOK_SECRET="s")
    def test_non_command_comment_ignored(self):
        payload = {
            "action": "created",
            "issue": {"number": 7, "pull_request": {"url": "x"}},
            "comment": {"body": "looks good to me"},
            "repository": {"full_name": "o/r"},
        }
        resp = self._signed("issue_comment", payload)
        self.assertEqual(resp.json(), {"ignored": True})

    @override_settings(PRCHECK_GITHUB_WEBHOOK_SECRET="s")
    def test_command_on_plain_issue_ignored(self):
        payload = {
            "action": "created",
            "issue": {"number": 7},  # no pull_request -> not a PR
            "comment": {"body": "/prcheck"},
            "repository": {"full_name": "o/r"},
        }
        resp = self._signed("issue_comment", payload)
        self.assertEqual(resp.json(), {"ignored": True})

    def test_check_run_maps_verdict_to_conclusion(self):
        seen = {}

        def fake_patch(self, path, body):
            seen["conclusion"] = body.get("conclusion")
            return {}

        api = gh.GitHubAPI(repo="o/r", token="t")
        with mock.patch.object(gh.GitHubAPI, "patch", fake_patch):
            gh.complete_check_run(api, 123, "block", [], [])
        self.assertEqual(seen["conclusion"], "failure")
