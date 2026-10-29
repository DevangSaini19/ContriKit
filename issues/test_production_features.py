"""
Production-readiness tests for the verified solved-history features.

These complement ``issues/tests.py`` (which covers the functional behavior) by
pinning the things that break specifically on a *deployed* instance:

- the database itself enforces one row per (user, issue);
- the dashboard never leaks another user's history and never crashes on
  retention-nulled rows;
- the deployed templates really contain the new sections;
- ``manage.py check_features`` reports green on a healthy database, and
  reports red when a feature is actually missing.

They use only in-memory SQLite and mocked data, so no GitHub credentials or
network access are required.
"""

import json
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.db.utils import IntegrityError
from django.test import TestCase
from django.urls import reverse

from core.management.commands import check_features
from issues.models import Issue, SolvedIssue
from issues.tests import SolvedMLFixturesMixin


class DashboardProductionTests(SolvedMLFixturesMixin, TestCase):
    """The dashboard section as it behaves with real, mixed production data."""

    def setUp(self):
        self.editor = self.make_user("prod_editor", role="editor")
        self.owner = self.make_user("prod_owner", github_username="owner")
        self.other = self.make_user("prod_other", github_username="other")
        self.repo = self.make_repo(self.editor, "prod-repo", language="Python")
        self.repo_b = self.make_repo(self.editor, "prod-repo-b", language="Go")
        self.issues = [
            self.make_issue(self.repo, self.editor, f"Production issue {n}")
            for n in range(1, 7)
        ]

    def _rows(self, user):
        self.client.force_login(user)
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 200)
        return response, list(response.context["solved_issues"])

    def test_dashboard_shows_at_most_four_most_recent(self):
        for issue in self.issues:
            self.verified_solve(self.owner, issue)
        response, rows = self._rows(self.owner)
        self.assertEqual(len(rows), 4)
        self.assertEqual(response.context["solved_count"], 6)
        self.assertEqual(
            [r.solved_at for r in rows],
            sorted((r.solved_at for r in rows), reverse=True),
        )

    def test_dashboard_section_is_user_scoped_with_mixed_data(self):
        for issue in self.issues[:3]:
            self.verified_solve(self.owner, issue)
        for issue in self.issues[3:]:
            self.verified_solve(self.other, issue)

        response, rows = self._rows(self.owner)
        self.assertEqual({r.user_id for r in rows}, {self.owner.id})
        for issue in self.issues[3:]:
            self.assertNotContains(response, issue.title)
        for issue in self.issues[:3]:
            self.assertContains(response, issue.title)

    def test_unverified_rows_are_never_rendered_or_counted(self):
        SolvedIssue.objects.create(
            user=self.owner, issue=self.issues[0], is_verified=False
        )
        self.verified_solve(self.owner, self.issues[1])
        response, rows = self._rows(self.owner)
        self.assertEqual([r.issue_id for r in rows], [self.issues[1].id])
        self.assertEqual(response.context["solved_count"], 1)
        self.assertNotContains(response, self.issues[0].title)

    def test_closed_issue_kept_with_badge_but_not_in_open_listing(self):
        self.verified_solve(self.owner, self.issues[0])
        self.issues[0].status = "closed"
        self.issues[0].save()

        response, rows = self._rows(self.owner)
        self.assertEqual([r.issue_id for r in rows], [self.issues[0].id])
        self.assertContains(response, "Closed")
        self.assertNotContains(self.client.get("/issues/"), self.issues[0].title)

    def test_retention_nulled_rows_are_skipped_not_fatal(self):
        self.verified_solve(self.owner, self.issues[0])
        self.verified_solve(self.owner, self.issues[1])
        SolvedIssue.objects.filter(user=self.owner, issue=self.issues[0]).update(
            issue=None
        )
        response, rows = self._rows(self.owner)
        self.assertEqual([r.issue_id for r in rows], [self.issues[1].id])
        self.assertEqual(response.context["solved_count"], 1)

    def test_dashboard_does_not_issue_a_query_per_row(self):
        for issue in self.issues[:4]:
            self.verified_solve(self.owner, issue)
        self.client.force_login(self.owner)
        with self.assertNumQueries(10):
            self.client.get("/dashboard/")

    def test_repo_relation_is_prefetched_for_every_row(self):
        for issue in self.issues[:4]:
            self.verified_solve(self.owner, issue)
        _, rows = self._rows(self.owner)
        for row in rows:
            self.assertIn("repo", row.issue._state.fields_cache)
            self.assertIsNotNone(row.issue.repo.name)


class SolvedHistoryIntegrityTests(SolvedMLFixturesMixin, TestCase):
    """Database-level guarantees that keep verified history trustworthy."""

    def setUp(self):
        self.editor = self.make_user("integrity_editor", role="editor")
        self.user = self.make_user("integrity_user", github_username="octo")
        self.repo = self.make_repo(self.editor, "integrity-repo")
        self.issue = self.make_issue(self.repo, self.editor, "Integrity issue")
        self.other_issue = self.make_issue(self.repo, self.editor, "Second issue")

    def test_unique_constraint_exists_at_database_level(self):
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(
                cursor, SolvedIssue._meta.db_table
            )
        unique_pairs = [
            info
            for info in constraints.values()
            if info.get("unique")
            and set(info.get("columns") or []) == {"user_id", "issue_id"}
        ]
        declared = any(
            getattr(c, "name", "") == "unique_user_issue_solved"
            for c in SolvedIssue._meta.constraints
        )
        self.assertTrue(unique_pairs or declared)

    def test_repeated_verification_cannot_create_a_second_row(self):
        self.verified_solve(self.user, self.issue)
        for _ in range(3):
            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    self.verified_solve(self.user, self.issue)
        self.assertEqual(
            SolvedIssue.objects.filter(user=self.user, issue=self.issue).count(), 1
        )

    def test_multiple_users_can_hold_the_same_issue(self):
        neighbour = self.make_user("integrity_neighbour", github_username="octo2")
        self.verified_solve(self.user, self.issue)
        self.verified_solve(neighbour, self.issue)
        self.assertEqual(
            SolvedIssue.objects.filter(issue=self.issue, is_verified=True).count(), 2
        )

    def test_issue_deletion_preserves_history_without_fk_error(self):
        record = self.verified_solve(self.user, self.issue)
        self.issue.delete()
        record.refresh_from_db()
        self.assertIsNone(record.issue)
        self.assertTrue(record.is_verified)
        self.assertEqual(record.user_id, self.user.id)

    def test_solved_at_is_set_automatically_and_ordered_newest_first(self):
        first = self.verified_solve(self.user, self.issue)
        second = self.verified_solve(self.user, self.other_issue)
        self.assertIsNotNone(first.solved_at)
        self.assertGreaterEqual(second.solved_at, first.solved_at)
        self.assertEqual(
            list(
                SolvedIssue.objects.filter(user=self.user).values_list(
                    "id", flat=True
                )
            ),
            [second.id, first.id],
        )


class DeployedTemplateMarkerTests(TestCase):
    """The rendered deployable files must contain the new feature sections."""

    def test_dashboard_template_has_solved_panel_and_empty_state(self):
        source = (
            Path(settings.BASE_DIR) / "templates" / "core" / "dashboard.html"
        ).read_text(encoding="utf-8")
        self.assertIn("Your Solved Issues", source)
        self.assertIn("No solved issues yet", source)
        self.assertIn("item.issue.status == 'closed'", source)

    def test_issue_detail_template_has_closed_branch(self):
        source = (
            Path(settings.BASE_DIR) / "templates" / "issues" / "issue_detail.html"
        ).read_text(encoding="utf-8")
        self.assertIn("This issue is closed", source)
        self.assertIn("{% if issue.status == 'open' %}", source)


class CheckFeaturesCommandTests(SolvedMLFixturesMixin, TestCase):
    """The production self-check must pass when healthy and fail when broken."""

    def setUp(self):
        self.editor = self.make_user("check_editor", role="editor")
        self.user = self.make_user("check_user", github_username="checker")
        self.repo = self.make_repo(self.editor, "check-repo")
        self.issue = self.make_issue(self.repo, self.editor, "Check issue")

    def _run(self, *args):
        out = StringIO()
        call_command("check_features", *args, stdout=out)
        return out.getvalue()

    def test_command_passes_on_healthy_database(self):
        self.verified_solve(self.user, self.issue)
        output = self._run()
        self.assertIn("[PASS] migrations", output)
        self.assertIn("[PASS] unique_constraint", output)
        self.assertIn("[PASS] dashboard_queries", output)
        self.assertIn("[PASS] templates", output)
        self.assertIn("All checks passed.", output)

    def test_command_passes_with_empty_solved_history(self):
        output = self._run()
        self.assertIn("All checks passed.", output)

    def test_json_output_is_machine_readable(self):
        self.verified_solve(self.user, self.issue)
        payload = json.loads(self._run("--json"))
        self.assertTrue(all(c["ok"] for c in payload["checks"]))
        self.assertIsInstance(payload["findings"], list)

    def test_legacy_rows_are_reported_but_do_not_fail(self):
        SolvedIssue.objects.create(
            user=self.user, issue=self.issue, is_verified=False
        )
        SolvedIssue.objects.create(user=self.user, issue=None, is_verified=True)
        payload = json.loads(self._run("--json"))
        self.assertTrue(all(c["ok"] for c in payload["checks"]))
        findings = " ".join(payload["findings"])
        self.assertIn("unverified legacy rows", findings)
        self.assertIn("retention-nulled", findings)

    def test_command_fails_when_dashboard_marker_is_missing(self):
        with self.settings(BASE_DIR=Path(settings.BASE_DIR) / "does-not-exist"):
            with self.assertRaises(CommandError):
                self._run()

    def test_duplicate_detection_reports_broken_invariant(self):
        original = check_features._check_duplicates
        check_features._check_duplicates = lambda: (
            False,
            "1 duplicate (user, issue) group(s)",
        )
        self.addCleanup(setattr, check_features, "_check_duplicates", original)
        with self.assertRaises(CommandError):
            self._run()

    def test_dashboard_query_check_detects_cross_user_leak(self):
        self.verified_solve(self.user, self.issue)
        # A second user's row makes the leaky query return foreign data.
        neighbour = self.make_user("check_neighbour", github_username="check2")
        second_issue = self.make_issue(self.repo, self.editor, "Neighbour issue")
        self.verified_solve(neighbour, second_issue)
        original = check_features._dashboard_rows_for

        def leaky(user):
            return list(
                SolvedIssue.objects.filter(is_verified=True).select_related(
                    "issue", "issue__repo"
                )
            )

        check_features._dashboard_rows_for = leaky
        self.addCleanup(
            setattr, check_features, "_dashboard_rows_for", original
        )
        with self.assertRaises(CommandError):
            self._run()


class SolverIsolationOnPagesTests(SolvedMLFixturesMixin, TestCase):
    """'Solved' badges must be per-user on list, landing, and detail pages."""

    def setUp(self):
        self.editor = self.make_user("iso_editor", role="editor")
        self.alice = self.make_user("iso_alice", github_username="alice")
        self.bob = self.make_user("iso_bob", github_username="bob")
        self.repo = self.make_repo(self.editor, "iso-repo")
        self.issue = self.make_issue(self.repo, self.editor, "Isolation issue")
        self.verified_solve(self.alice, self.issue)

    def test_issue_list_badge_only_for_solver(self):
        self.client.force_login(self.alice)
        self.assertContains(self.client.get("/issues/"), "Solved")
        self.client.force_login(self.bob)
        self.assertNotContains(self.client.get("/issues/"), "Solved")

    def test_landing_page_badge_only_for_solver(self):
        self.client.force_login(self.bob)
        landing = self.client.get("/")
        self.assertNotIn(self.issue.id, landing.context["solved_ids"])
        self.client.force_login(self.alice)
        landing = self.client.get("/")
        self.assertIn(self.issue.id, landing.context["solved_ids"])

    def test_issue_detail_verified_banner_only_for_solver(self):
        self.client.force_login(self.alice)
        self.assertContains(
            self.client.get(reverse("issue_detail", args=[self.issue.id])),
            "Verified GitHub contribution",
        )
        self.client.force_login(self.bob)
        self.assertNotContains(
            self.client.get(reverse("issue_detail", args=[self.issue.id])),
            "Verified GitHub contribution",
        )
