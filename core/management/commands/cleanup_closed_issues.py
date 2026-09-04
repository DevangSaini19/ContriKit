"""
Cleanup closed issues after retention period.

Safe production design:
- Only deletes issues where status='closed' AND closed_at is not null
  AND closed_at <= now - retention_days.
- For legacy closed issues with closed_at=None, falls back to updated_at
  (or created_at) so old data is still eventually cleaned, but only after
  the same retention window (conservative).
- Never deletes open issues.
- Preserves SolvedIssue history: SolvedIssue.issue uses SET_NULL, so deleting
  the issue just nulls the FK — no DB error, history remains.
- Dry-run supported via --dry-run.
- Configurable retention via settings.CLOSED_ISSUE_RETENTION_DAYS (env var).

PythonAnywhere deployment:
  Schedule as a daily task in PythonAnywhere dashboard:
    Task command: /home/<user>/.virtualenvs/venv/bin/python /home/<user>/contribkit/manage.py cleanup_closed_issues
  Or run manually: python manage.py cleanup_closed_issues --dry-run

"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from issues.models import Issue


class Command(BaseCommand):
    help = "Remove closed issues older than retention period (preserves solved history via SET_NULL)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be deleted without actually deleting.",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help="Override retention days (default from settings.CLOSED_ISSUE_RETENTION_DAYS).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        retention_days = options["days"]
        if retention_days is None:
            retention_days = getattr(settings, "CLOSED_ISSUE_RETENTION_DAYS", 30)

        cutoff = timezone.now() - timedelta(days=retention_days)

        # Primary criterion: closed_at <= cutoff
        # Legacy fallback: closed_at is null but status closed and updated_at <= cutoff
        # This ensures old closed issues without timestamp are still cleaned after same window,
        # using the safest available timestamp (updated_at or created_at).
        qs = Issue.objects.filter(status="closed").filter(
            Q(closed_at__lte=cutoff) | Q(closed_at__isnull=True, updated_at__lte=cutoff)
        )

        count = qs.count()

        if dry_run:
            self.stdout.write(f"[DRY RUN] Would delete {count} closed issue(s) older than {retention_days} days (cutoff: {cutoff.isoformat()})")
            for issue in qs.select_related("repo")[:20]:
                self.stdout.write(f"  - #{issue.id} {issue.title[:60]} (status={issue.status}, closed_at={issue.closed_at}, updated_at={issue.updated_at})")
            if count > 20:
                self.stdout.write(f"  ... and {count - 20} more")
            self.stdout.write(self.style.SUCCESS("Dry run complete — no issues deleted."))
            return

        if count == 0:
            self.stdout.write(self.style.SUCCESS(f"No closed issues older than {retention_days} days to clean up (cutoff: {cutoff.date()})."))
            return

        # Capture ids for logging before delete (qs will be gone after)
        ids = list(qs.values_list("id", flat=True))
        # Delete — SolvedIssue rows become NULL via SET_NULL, no error
        deleted, details = qs.delete()

        self.stdout.write(self.style.SUCCESS(f"Deleted {count} closed issue(s) older than {retention_days} days (cutoff: {cutoff.date()})."))
        self.stdout.write(f"Details: {details}")
