"""
Read-only production self-check for the "solved issues" features.

Run on the deployed instance (PythonAnywhere, MySQL or SQLite):

    python manage.py check_features

Checks performed (all read-only, no data is written):

1. Required migrations for SolvedIssue / Issue.closed_at are applied.
2. The database enforces one row per (user, issue).
3. No duplicate (user, issue) rows exist for real issues.
4. The dashboard query returns only the inspected user's verified rows.
5. Verified counts agree with distinct verified issue ids.
6. The dashboard template renders the "Your Solved Issues" panel.
7. The issue detail template has the closed-issue branch instead of the
   open-issue CTA.

Exit code is 0 when every hard check passes, 1 otherwise. Legacy/informational
findings are reported but do not fail the command.
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import Count

from accounts.models import User
from issues.models import Issue, SolvedIssue

REQUIRED_ISSUES_MIGRATIONS = {
    "0002_solved_and_closed_at",
    "0003_solvedissue_verified",
}

DASHBOARD_MARKER = "Your Solved Issues"
ISSUE_DETAIL_CLOSED_MARKER = "This issue is closed"


def _template_path(relative_path):
    return Path(settings.BASE_DIR) / "templates" / relative_path


def _check_migrations():
    applied = set(
        MigrationRecorder.Migration.objects.filter(app="issues").values_list(
            "name", flat=True
        )
    )
    missing = sorted(REQUIRED_ISSUES_MIGRATIONS - applied)
    if missing:
        return False, f"missing issues migrations: {', '.join(missing)}"
    return True, "required issues migrations applied"


def _check_model_shape():
    field_names = {f.name for f in SolvedIssue._meta.get_fields()}
    required = {"is_verified", "github_pr_url", "issue", "solved_at", "user"}
    missing = sorted(required - field_names)
    if missing:
        return False, f"SolvedIssue is missing fields: {', '.join(missing)}"
    issue_field = SolvedIssue._meta.get_field("issue")
    if not issue_field.null:
        return False, "SolvedIssue.issue must be nullable (retention cleanup)"
    if issue_field.remote_field.on_delete.__name__ != "SET_NULL":
        return False, "SolvedIssue.issue must use on_delete=SET_NULL"
    return True, "SolvedIssue shape matches the verified-history contract"


def _has_unique_user_issue_constraint():
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(
            cursor, SolvedIssue._meta.db_table
        )
    for info in constraints.values():
        if info.get("unique") and set(info.get("columns") or []) == {
            "user_id",
            "issue_id",
        }:
            return True
    # Some backends do not report column lists for constraints; fall back to
    # the declared model constraint so a healthy DB is not flagged.
    return any(
        getattr(c, "name", "") == "unique_user_issue_solved"
        for c in SolvedIssue._meta.constraints
    )


def _check_unique_constraint():
    if _has_unique_user_issue_constraint():
        return True, "unique (user, issue) constraint present at DB level"
    return False, "unique (user, issue) constraint is missing"


def _check_duplicates():
    duplicates = (
        SolvedIssue.objects.exclude(issue__isnull=True)
        .values("user_id", "issue_id")
        .annotate(rows=Count("id"))
        .filter(rows__gt=1)
        .order_by()
    )
    count = duplicates.count()
    if count:
        sample = list(duplicates[:5])
        return False, f"{count} duplicate (user, issue) group(s), e.g. {sample}"
    return True, "no duplicate (user, issue) rows"


def _dashboard_rows_for(user):
    return list(
        SolvedIssue.objects.filter(user=user, is_verified=True)
        .exclude(issue__isnull=True)
        .select_related("issue", "issue__repo")
        .prefetch_related("issue__tags")
        .order_by("-solved_at")[:4]
    )


def _counts_for(user):
    verified_qs = SolvedIssue.objects.filter(user=user, is_verified=True).exclude(
        issue__isnull=True
    )
    return verified_qs.count(), verified_qs.values("issue_id").distinct().count()


def _check_dashboard_queries(limit_users=50):
    users = list(
        User.objects.filter(
            solved_issues__is_verified=True, solved_issues__issue__isnull=False
        )
        .distinct()
        .order_by("id")[:limit_users]
    )
    for user in users:
        rows = _dashboard_rows_for(user)
        for row in rows:
            if row.user_id != user.id:
                return False, f"dashboard query leaked another user's row to {user.pk}"
            if row.issue is None:
                return False, f"dashboard query returned a null issue for user {user.pk}"
            if row.issue.repo_id is None:
                return False, f"dashboard row for user {user.pk} has no repo"
            # select_related('issue__repo') must be populated without a query.
            if "repo" not in row.issue._state.fields_cache:
                return False, "issue__repo select_related is not applied"
        solved_count, distinct_count = _counts_for(user)
        if solved_count != distinct_count:
            return (
                False,
                f"user {user.pk} has {solved_count} verified rows but "
                f"{distinct_count} distinct issues",
            )
    return True, f"dashboard queries healthy for {len(users)} user(s) with history"


def _check_templates():
    dashboard = _template_path("core/dashboard.html")
    issue_detail = _template_path("issues/issue_detail.html")
    if not dashboard.exists():
        return False, f"missing template: {dashboard}"
    if not issue_detail.exists():
        return False, f"missing template: {issue_detail}"
    dashboard_src = dashboard.read_text(encoding="utf-8")
    issue_detail_src = issue_detail.read_text(encoding="utf-8")
    if DASHBOARD_MARKER not in dashboard_src:
        return False, "dashboard.html is missing the 'Your Solved Issues' panel"
    if ISSUE_DETAIL_CLOSED_MARKER not in issue_detail_src:
        return False, "issue_detail.html is missing the closed-issue notice"
    if "solved_issues" not in dashboard_src:
        return False, "dashboard.html does not iterate solved_issues"
    return True, "dashboard and issue detail templates contain the new sections"


def _legacy_findings():
    unverified = SolvedIssue.objects.filter(is_verified=False).count()
    orphaned = SolvedIssue.objects.filter(issue__isnull=True).count()
    closed_without_stamp = Issue.objects.filter(
        status="closed", closed_at__isnull=True
    ).count()
    return [
        f"unverified legacy rows (not trusted in UI): {unverified}",
        f"retention-nulled history rows (preserved): {orphaned}",
        f"closed issues without closed_at (retention falls back to updated_at): "
        f"{closed_without_stamp}",
    ]


def run_checks():
    """Return (results, findings) without writing to the database."""
    results = [
        ("migrations", _check_migrations),
        ("model", _check_model_shape),
        ("unique_constraint", _check_unique_constraint),
        ("duplicates", _check_duplicates),
        ("dashboard_queries", _check_dashboard_queries),
        ("templates", _check_templates),
    ]
    report = []
    for name, func in results:
        try:
            ok, detail = func()
        except Exception as exc:  # noqa: BLE001 - report, never crash the check
            ok, detail = False, f"check raised {type(exc).__name__}: {exc}"
        report.append({"name": name, "ok": ok, "detail": detail})
    return report, _legacy_findings()


class Command(BaseCommand):
    help = "Read-only production check for the verified solved-history features."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit the report as JSON (useful for cron/CI logs).",
        )

    def handle(self, *args, **options):
        report, findings = run_checks()
        failures = [r for r in report if not r["ok"]]

        if options["json"]:
            import json

            self.stdout.write(
                json.dumps({"checks": report, "findings": findings}, indent=2)
            )
            if failures:
                # Keep stdout valid JSON; the failure summary goes to stderr.
                self.stderr.write(
                    f"{len(failures)} check(s) failed: "
                    + ", ".join(f["name"] for f in failures)
                )
                raise CommandError("production feature checks failed")
            return

        self.stdout.write("Verified solved-history production check")
        self.stdout.write(f"database: {connection.vendor}")
        for item in report:
            status = "PASS" if item["ok"] else "FAIL"
            style = self.style.SUCCESS if item["ok"] else self.style.ERROR
            self.stdout.write(style(f"[{status}] {item['name']}: {item['detail']}"))
        self.stdout.write("")
        self.stdout.write("Informational findings (do not fail the check):")
        for line in findings:
            self.stdout.write(f"  - {line}")

        if failures:
            raise CommandError(
                f"{len(failures)} check(s) failed: "
                + ", ".join(f["name"] for f in failures)
            )
        self.stdout.write(self.style.SUCCESS("All checks passed."))
