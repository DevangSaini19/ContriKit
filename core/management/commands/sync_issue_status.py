"""
Synchronize local Issue open/closed status with GitHub.

Usage:
    python manage.py sync_issue_status --dry-run
    python manage.py sync_issue_status
    python manage.py sync_issue_status --issue-id 42
    python manage.py sync_issue_status --repo pallets/flask
    python manage.py sync_issue_status --include-all
    python manage.py sync_issue_status --verbose

Exit codes:
    0  all selected issues were verified (updates and no-ops alike)
    1  at least one issue could not be verified (invalid URL, API failure,
       rate limit, bad payload, ...). Successful updates may still have been
       written; the summary always reports exactly what happened.

PythonAnywhere deployment: schedule as a daily task, e.g.
    /home/<user>/.virtualenvs/venv/bin/python /home/<user>/contrikit/manage.py sync_issue_status
"""

import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from issues.models import Issue
from issues.github_sync import (
    REASON_RATE_LIMITED,
    sync_issue,
)

logger = logging.getLogger(__name__)

# Human-readable labels for failure buckets shown in the summary.
FAILURE_LABELS = {
    "invalid_url": "invalid GitHub URL",
    "unauthorized": "GitHub authentication failed (401)",
    "forbidden": "GitHub forbidden (403)",
    "rate_limited": "GitHub rate limit hit (retry later or set GITHUB_PAT)",
    "not_found": "issue/PR not found (404)",
    "server_error": "GitHub server error (5xx)",
    "network_error": "network error",
    "timeout": "request timed out",
    "invalid_json": "malformed JSON response",
    "malformed_response": "unexpected response shape",
    "missing_state": "response missing 'state'",
    "invalid_state": "unsupported 'state' value",
    "invalid_timestamp": "invalid 'closed_at' timestamp",
}


class Command(BaseCommand):
    help = "Synchronize local issue status (open/closed) with GitHub."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing to the database.",
        )
        parser.add_argument(
            "--issue-id",
            type=int,
            default=None,
            help="Synchronize a single local issue by primary key.",
        )
        parser.add_argument(
            "--repo",
            default=None,
            metavar="OWNER/REPO",
            help="Only synchronize issues whose GitHub repository is OWNER/REPO.",
        )
        parser.add_argument(
            "--include-all",
            action="store_true",
            help="Include locally closed issues so GitHub reopens are detected.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Print one line per issue with the decision taken.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        issue_id = options["issue_id"]
        repo_filter = (options["repo"] or "").strip()
        include_all = options["include_all"]
        verbose = options["verbose"]

        if issue_id is not None and repo_filter:
            raise CommandError("Use either --issue-id or --repo, not both.")

        if repo_filter and repo_filter.count("/") != 1:
            raise CommandError("--repo must be in OWNER/REPO form, e.g. pallets/flask.")

        qs = Issue.objects.all().select_related("repo")
        if issue_id is not None:
            qs = qs.filter(pk=issue_id)
            if not qs.exists():
                raise CommandError(f"No issue with id {issue_id}.")
        elif not include_all:
            # Default: open rows are the ones that can change to closed.
            # --include-all adds closed rows so reopens are detected too.
            qs = qs.filter(status="open")
        qs = qs.order_by("pk")

        issues = list(qs)
        if repo_filter and issues:
            wanted = repo_filter.lower()
            issues = [
                issue
                for issue in issues
                if self._issue_repo_slug(issue) == wanted
            ]

        if not issues:
            self.stdout.write("No issues matched the selection. Nothing to do.")
            return

        stats = {
            "checked": 0,
            "updated": 0,
            "unchanged": 0,
            "would_update": 0,
            "skipped": 0,
            "failed": 0,
        }
        failures = {}
        changed_rows = []

        for issue in issues:
            stats["checked"] += 1
            try:
                with transaction.atomic():
                    result = sync_issue(issue, dry_run=dry_run)
            except Exception as exc:  # never let one bad row kill the run
                logger.exception("Sync crashed for issue %s", issue.pk)
                result = None
                reason = "crash"
                stats["failed"] += 1
                failures[reason] = failures.get(reason, 0) + 1
                self.stdout.write(
                    self.style.ERROR(
                        f"  #{issue.pk} crashed: {type(exc).__name__}: {exc}"
                    )
                )
                continue

            if result.action == "updated":
                stats["updated"] += 1
                changed_rows.append((issue, result))
                if verbose:
                    self.stdout.write(
                        f"  #{issue.pk} updated: {self._describe(result.changes)}"
                    )
            elif result.action == "would_update":
                stats["would_update"] += 1
                changed_rows.append((issue, result))
                if verbose:
                    self.stdout.write(
                        f"  #{issue.pk} WOULD UPDATE: {self._describe(result.changes)}"
                    )
            elif result.action == "unchanged":
                stats["unchanged"] += 1
                if verbose:
                    self.stdout.write(f"  #{issue.pk} unchanged")
            elif result.action == "skipped":
                stats["skipped"] += 1
                if verbose:
                    self.stdout.write(
                        f"  #{issue.pk} skipped ({result.reason}): {result.message}"
                    )
            else:
                stats["failed"] += 1
                failures[result.reason] = failures.get(result.reason, 0) + 1
                self.stdout.write(
                    self.style.WARNING(
                        f"  #{issue.pk} could not be verified "
                        f"({FAILURE_LABELS.get(result.reason, result.reason)}): "
                        f"{result.message}"
                    )
                )

        self._write_summary(
            stats=stats,
            failures=failures,
            changed_rows=changed_rows,
            dry_run=dry_run,
            repo_filter=repo_filter,
            include_all=include_all,
        )

        if stats["failed"]:
            raise CommandError(
                f"{stats['failed']} issue(s) could not be verified; "
                "their local status was left unchanged."
            )

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _issue_repo_slug(issue):
        from issues.github_sync import parse_github_item_url

        ref = parse_github_item_url(issue.github_issue_url)
        if not ref:
            return ""
        return f"{ref.owner}/{ref.repo}".lower()

    @staticmethod
    def _describe(changes):
        parts = []
        if "status" in changes:
            parts.append(f"status -> {changes['status']}")
        if "closed_at" in changes:
            value = changes["closed_at"]
            if value is None:
                parts.append("closed_at -> None")
            else:
                parts.append(f"closed_at -> {value.isoformat()}")
        return ", ".join(parts) if parts else "no field changes"

    def _write_summary(
        self, *, stats, failures, changed_rows, dry_run, repo_filter, include_all
    ):
        mode = "DRY RUN — no changes written" if dry_run else "APPLIED"
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"sync_issue_status ({mode})"))
        selection = "open + closed" if include_all else "open only"
        self.stdout.write(f"  selection:          {selection}")
        if repo_filter:
            self.stdout.write(f"  repo filter: {repo_filter}")
        self.stdout.write(f"  checked:            {stats['checked']}")
        if dry_run:
            self.stdout.write(f"  would update:       {stats['would_update']}")
        else:
            self.stdout.write(f"  updated:            {stats['updated']}")
        self.stdout.write(f"  unchanged:          {stats['unchanged']}")
        self.stdout.write(f"  skipped (demo):     {stats['skipped']}")
        self.stdout.write(f"  unverified/failed:  {stats['failed']}")

        if failures:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("Failures by reason:"))
            for reason, count in sorted(failures.items()):
                label = FAILURE_LABELS.get(reason, reason)
                self.stdout.write(f"  {label}: {count}")
            if REASON_RATE_LIMITED in failures:
                self.stdout.write(
                    "  hint: set GITHUB_PAT to raise the rate limit from 60 to 5000 req/hr."
                )

        if changed_rows and dry_run:
            self.stdout.write("")
            self.stdout.write("Issues that would change:")
            for issue, result in changed_rows:
                self.stdout.write(
                    f"  #{issue.pk} {issue.title[:60]}: {self._describe(result.changes)}"
                )

        if not dry_run and stats["updated"]:
            self.stdout.write(
                self.style.SUCCESS(f"Updated {stats['updated']} issue(s).")
            )
        if stats["failed"]:
            self.stdout.write(
                self.style.ERROR(
                    f"{stats['failed']} issue(s) could not be verified; "
                    "their status/closed_at were left untouched."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("All selected issues verified."))
