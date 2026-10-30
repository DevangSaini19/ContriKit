"""
Tests for GitHub issue status synchronization.

Everything here is mocked: no network access and no GitHub credentials are
required. The tests pin the properties that caused the production bug:

- a closed GitHub issue is reflected locally (so "Ready to tackle this issue?"
  disappears) and an open one is not;
- GitHub's own ``closed_at`` is stored, never ``timezone.now()``;
- reopen is detected (including for locally closed rows via ``--include-all``);
- any API problem (401/403/404/5xx/429/timeout/bad JSON/bad state) leaves the
  local row untouched and is reported as a failure, never "open";
- demo rows and non-GitHub URLs are never queried;
- sync is idempotent and ``--dry-run`` writes nothing.
"""

import json
from io import StringIO
from unittest.mock import patch

import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from issues.github_sync import (
    REASON_DEMO,
    REASON_INVALID_STATE,
    REASON_INVALID_TIMESTAMP,
    REASON_INVALID_URL,
    REASON_MISSING_STATE,
    REASON_NOT_FOUND,
    REASON_RATE_LIMITED,
    REASON_TIMEOUT,
    REASON_UNAUTHORIZED,
    fetch_github_item,
    parse_github_item_url,
    sync_issue,
)
from issues.models import Issue
from issues.tests import SolvedMLFixturesMixin


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, bad_json=False):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("no json")
        return self._payload


def getter_for(response):
    calls = []

    def getter(url, params=None, timeout=None):
        calls.append({"url": url, "params": params, "timeout": timeout})
        return response

    getter.calls = calls
    return getter


def getter_raising(exc):
    def getter(url, params=None, timeout=None):
        raise exc

    return getter


class ParseUrlTests(TestCase):
    def test_issue_and_pull_urls(self):
        ref = parse_github_item_url("https://github.com/pallets/flask/issues/123")
        self.assertIsNotNone(ref)
        self.assertEqual((ref.owner, ref.repo, ref.number, ref.kind),
                         ("pallets", "flask", 123, "issues"))
        ref = parse_github_item_url("https://github.com/pallets/flask/pull/9/")
        self.assertEqual((ref.kind, ref.number), ("pull", 9))

    def test_accepts_query_string_and_fragment(self):
        ref = parse_github_item_url(
            "https://github.com/a/b/issues/5?foo=bar#issuecomment-1"
        )
        self.assertEqual(ref.number, 5)

    def test_rejects_non_github_and_malformed(self):
        for url in [
            None,
            "",
            "not-a-url",
            "https://gitlab.com/a/b/issues/1",
            "https://github.com/a/b",
            "https://github.com/a/b/issues/notanumber",
            "https://github.com/a/b/issues/0",
            "ftp://github.com/a/b/issues/1",
            "https://github.com/a/b/tree/main",
            "https://github.com/a/b/issues/1/extra",
        ]:
            with self.subTest(url=url):
                self.assertIsNone(parse_github_item_url(url))


class FetchTests(TestCase):
    def _ref(self):
        return parse_github_item_url("https://github.com/a/b/issues/1")

    def test_open_payload(self):
        resp = FakeResponse(payload={"state": "open", "closed_at": None,
                                     "title": "T", "html_url": "u"})
        result = fetch_github_item(self._ref(), getter=getter_for(resp))
        self.assertTrue(result.ok)
        self.assertEqual(result.state, "open")
        self.assertIsNone(result.closed_at)

    def test_closed_payload_parses_closed_at(self):
        resp = FakeResponse(payload={
            "state": "closed",
            "closed_at": "2024-03-01T10:20:30Z",
            "title": "T",
        })
        result = fetch_github_item(self._ref(), getter=getter_for(resp))
        self.assertTrue(result.ok)
        self.assertEqual(result.state, "closed")
        self.assertEqual(result.closed_at.year, 2024)
        self.assertEqual(result.closed_at.month, 3)

    @override_settings(GITHUB_PAT="secret-token")
    def test_uses_authorization_header_when_configured(self):
        captured = {}

        def fake_get(url, headers=None, params=None, timeout=None):
            captured["headers"] = headers
            return FakeResponse(payload={"state": "open", "closed_at": None})

        with patch("issues.github_sync.requests.get", side_effect=fake_get):
            result = fetch_github_item(self._ref())
        self.assertTrue(result.ok)
        self.assertEqual(captured["headers"]["Authorization"], "token secret-token")

    @override_settings(GITHUB_PAT="")
    def test_omits_authorization_without_token(self):
        captured = {}

        def fake_get(url, headers=None, params=None, timeout=None):
            captured["headers"] = headers
            return FakeResponse(payload={"state": "open", "closed_at": None})

        with patch("issues.github_sync.requests.get", side_effect=fake_get):
            fetch_github_item(self._ref())
        self.assertNotIn("Authorization", captured["headers"])

    def test_error_statuses_are_failures_not_open(self):
        cases = {
            401: REASON_UNAUTHORIZED,
            403: "forbidden",
            404: REASON_NOT_FOUND,
            500: "server_error",
            502: "server_error",
        }
        for code, reason in cases.items():
            with self.subTest(code=code):
                result = fetch_github_item(
                    self._ref(), getter=getter_for(FakeResponse(status_code=code))
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.reason, reason)

    def test_rate_limit_403_with_zero_remaining(self):
        resp = FakeResponse(status_code=403,
                            headers={"X-RateLimit-Remaining": "0"})
        result = fetch_github_item(self._ref(), getter=getter_for(resp))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, REASON_RATE_LIMITED)

    def test_429_is_rate_limited(self):
        result = fetch_github_item(
            self._ref(), getter=getter_for(FakeResponse(status_code=429))
        )
        self.assertEqual(result.reason, REASON_RATE_LIMITED)

    def test_timeout_and_network_error(self):
        result = fetch_github_item(
            self._ref(), getter=getter_raising(requests.Timeout())
        )
        self.assertEqual(result.reason, REASON_TIMEOUT)
        result = fetch_github_item(
            self._ref(), getter=getter_raising(requests.ConnectionError())
        )
        self.assertEqual(result.reason, "network_error")

    def test_invalid_json(self):
        result = fetch_github_item(
            self._ref(), getter=getter_for(FakeResponse(bad_json=True))
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "invalid_json")

    def test_malformed_and_missing_state(self):
        result = fetch_github_item(
            self._ref(), getter=getter_for(FakeResponse(payload=["nope"]))
        )
        self.assertEqual(result.reason, "malformed_response")
        result = fetch_github_item(
            self._ref(), getter=getter_for(FakeResponse(payload={"closed_at": None}))
        )
        self.assertEqual(result.reason, REASON_MISSING_STATE)

    def test_invalid_state_value(self):
        result = fetch_github_item(
            self._ref(), getter=getter_for(FakeResponse(payload={"state": "merged"}))
        )
        self.assertEqual(result.reason, REASON_INVALID_STATE)

    def test_invalid_closed_at_timestamp(self):
        result = fetch_github_item(
            self._ref(),
            getter=getter_for(
                FakeResponse(payload={"state": "closed", "closed_at": "not-a-date"})
            ),
        )
        self.assertEqual(result.reason, REASON_INVALID_TIMESTAMP)


class SyncIssueTests(SolvedMLFixturesMixin, TestCase):
    def setUp(self):
        self.editor = self.make_user("sync_editor", role="editor")
        self.repo = self.make_repo(self.editor, "sync-repo")
        self.issue = self.make_issue(self.repo, self.editor, "Real issue")
        Issue.objects.filter(pk=self.issue.pk).update(
            github_issue_url="https://github.com/pallets/flask/issues/123"
        )
        self.issue.refresh_from_db()

    def test_open_issue_stays_open(self):
        getter = getter_for(FakeResponse(payload={"state": "open", "closed_at": None}))
        result = sync_issue(self.issue, getter=getter)
        self.assertEqual(result.action, "unchanged")
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, "open")
        self.assertIsNone(self.issue.closed_at)
        self.assertIsNotNone(self.issue.github_synced_at)

    def test_closed_issue_marked_closed_with_github_timestamp(self):
        getter = getter_for(FakeResponse(payload={
            "state": "closed", "closed_at": "2023-11-05T08:00:00Z",
        }))
        before = timezone.now()
        result = sync_issue(self.issue, getter=getter)
        self.assertEqual(result.action, "updated")
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, "closed")
        self.assertIsNotNone(self.issue.closed_at)
        self.assertEqual(self.issue.closed_at.year, 2023)
        self.assertLess(self.issue.closed_at, before)

    def test_reopen_clears_closed_at(self):
        Issue.objects.filter(pk=self.issue.pk).update(
            status="closed", closed_at=timezone.now()
        )
        self.issue.refresh_from_db()
        self.assertIsNotNone(self.issue.closed_at)
        getter = getter_for(FakeResponse(payload={"state": "open", "closed_at": None}))
        result = sync_issue(self.issue, getter=getter)
        self.assertEqual(result.action, "updated")
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, "open")
        self.assertIsNone(self.issue.closed_at)

    def test_failure_leaves_row_untouched(self):
        getter = getter_for(FakeResponse(status_code=404))
        result = sync_issue(self.issue, getter=getter)
        self.assertTrue(result.failed)
        self.assertEqual(result.reason, REASON_NOT_FOUND)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, "open")
        self.assertIsNone(self.issue.github_synced_at)

    def test_demo_rows_never_hit_api(self):
        Issue.objects.filter(pk=self.issue.pk).update(is_demo=True)
        self.issue.refresh_from_db()
        getter = getter_for(FakeResponse(payload={"state": "closed",
                                                  "closed_at": "2024-01-01T00:00:00Z"}))
        result = sync_issue(self.issue, getter=getter)
        self.assertEqual(result.action, "skipped")
        self.assertEqual(result.reason, REASON_DEMO)
        self.assertEqual(getter.calls, [])

    def test_invalid_url_never_hit_api(self):
        Issue.objects.filter(pk=self.issue.pk).update(
            github_issue_url="https://example.com/not-github"
        )
        self.issue.refresh_from_db()
        getter = getter_for(FakeResponse(payload={"state": "closed"}))
        result = sync_issue(self.issue, getter=getter)
        self.assertTrue(result.failed)
        self.assertEqual(result.reason, REASON_INVALID_URL)
        self.assertEqual(getter.calls, [])

    def test_dry_run_writes_nothing(self):
        getter = getter_for(FakeResponse(payload={
            "state": "closed", "closed_at": "2023-01-01T00:00:00Z",
        }))
        result = sync_issue(self.issue, getter=getter, dry_run=True)
        self.assertEqual(result.action, "would_update")
        self.assertIn("status", result.changes)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, "open")
        self.assertIsNone(self.issue.github_synced_at)

    def test_idempotent_second_run_is_unchanged(self):
        resp = FakeResponse(payload={
            "state": "closed", "closed_at": "2023-01-01T00:00:00Z",
        })
        sync_issue(self.issue, getter=getter_for(resp))
        self.issue.refresh_from_db()
        result = sync_issue(self.issue, getter=getter_for(resp))
        self.assertEqual(result.action, "unchanged")


class SyncCommandTests(SolvedMLFixturesMixin, TestCase):
    def setUp(self):
        self.editor = self.make_user("cmd_editor", role="editor")
        self.repo = self.make_repo(self.editor, "cmd-repo")
        self.issue = self.make_issue(self.repo, self.editor, "Commando")
        Issue.objects.filter(pk=self.issue.pk).update(
            github_issue_url="https://github.com/owner/repo/issues/7"
        )
        self.issue.refresh_from_db()

    def _run(self, *args, getter=None):
        out = StringIO()
        with patch("issues.github_sync._default_get", getter):
            call_command("sync_issue_status", *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_closed_issue_updated(self):
        getter = getter_for(FakeResponse(payload={
            "state": "closed", "closed_at": "2024-02-02T00:00:00Z",
        }))
        out = self._run(getter=getter)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, "closed")
        self.assertIn("updated", out.lower())

    def test_dry_run_reports_without_writing(self):
        getter = getter_for(FakeResponse(payload={
            "state": "closed", "closed_at": "2024-02-02T00:00:00Z",
        }))
        out = self._run("--dry-run", getter=getter)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, "open")
        self.assertIn("would update", out.lower())

    def test_include_all_reopens_locally_closed_issue(self):
        Issue.objects.filter(pk=self.issue.pk).update(
            status="closed", closed_at=timezone.now()
        )
        getter = getter_for(FakeResponse(payload={"state": "open", "closed_at": None}))
        self._run("--include-all", getter=getter)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, "open")
        self.assertIsNone(self.issue.closed_at)

    def test_without_include_all_closed_row_not_selected(self):
        Issue.objects.filter(pk=self.issue.pk).update(status="closed")
        getter = getter_for(FakeResponse(payload={"state": "open", "closed_at": None}))
        out = self._run(getter=getter)
        self.assertEqual(getter.calls, [])
        self.assertIn("nothing to do", out.lower())

    def test_failure_raises_command_error_and_preserves_row(self):
        getter = getter_for(FakeResponse(status_code=503))
        with self.assertRaises(CommandError):
            self._run(getter=getter)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, "open")

    def test_repo_filter(self):
        other = self.make_issue(self.repo, self.editor, "Other repo issue")
        Issue.objects.filter(pk=other.pk).update(
            github_issue_url="https://github.com/another/project/issues/3"
        )
        getter = getter_for(FakeResponse(payload={"state": "closed",
                                                  "closed_at": "2024-01-01T00:00:00Z"}))
        self._run("--repo", "owner/repo", getter=getter)
        self.issue.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.issue.status, "closed")
        self.assertEqual(other.status, "open")
        self.assertEqual(len(getter.calls), 1)

    def test_demo_rows_skipped_by_command(self):
        Issue.objects.filter(pk=self.issue.pk).update(is_demo=True)
        getter = getter_for(FakeResponse(payload={"state": "closed",
                                                  "closed_at": "2024-01-01T00:00:00Z"}))
        out = self._run(getter=getter)
        self.assertEqual(getter.calls, [])
        self.assertIn("skipped", out.lower())

    def test_bad_repo_filter_rejected(self):
        with self.assertRaises(CommandError):
            call_command("sync_issue_status", "--repo", "no-slash", stdout=StringIO())
