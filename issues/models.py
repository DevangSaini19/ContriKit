from django.db import models
from django.conf import settings
from django.utils import timezone
from repos.models import Repo

class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True)
    slug = models.SlugField(unique=True)
    color = models.CharField(max_length=7, default="#6366f1")

    def __str__(self):
        return self.name

class Issue(models.Model):
    DIFFICULTY_CHOICES = [("beginner", "Beginner"), ("intermediate", "Intermediate")]
    STATUS_CHOICES = [("open", "Open"), ("closed", "Closed")]
    repo = models.ForeignKey(Repo, on_delete=models.CASCADE, related_name="issues")
    posted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    title = models.CharField(max_length=200)
    description = models.TextField()
    github_issue_url = models.URLField()
    difficulty = models.CharField(max_length=15, choices=DIFFICULTY_CHOICES)
    estimated_hours = models.DecimalField(max_digits=4, decimal_places=1)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="open")
    is_featured = models.BooleanField(default=False)
    view_count = models.PositiveIntegerField(default=0)
    tags = models.ManyToManyField(Tag, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # When the issue was closed — used for retention-based cleanup.
    # Null means currently open or legacy closed row without timestamp.
    closed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        # Skip closed_at logic when only updating unrelated fields (e.g. view_count)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "status" not in update_fields and "closed_at" not in update_fields:
            super().save(*args, **kwargs)
            return

        # Auto-manage closed_at whenever status changes.
        # If status becomes closed and closed_at is empty, stamp now.
        # If status becomes open again, clear closed_at so retention resets.
        if self.status == "closed" and self.closed_at is None:
            self.closed_at = timezone.now()
        elif self.status == "open" and self.closed_at is not None:
            # Check if this is a transition from closed to open
            # Only clear if previously closed — avoids wiping manually set future dates
            # We detect transition by looking at DB value if pk exists
            if self.pk:
                try:
                    prev = Issue.objects.only("status").get(pk=self.pk)
                    if prev.status == "closed":
                        self.closed_at = None
                except Issue.DoesNotExist:
                    self.closed_at = None
            else:
                self.closed_at = None
        super().save(*args, **kwargs)

    @property
    def is_open(self):
        return self.status == "open"

class SavedIssue(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE)
    saved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "issue")

    def __str__(self):
        return f"{self.user} saved {self.issue}"

class SolvedIssue(models.Model):
    """
    Records that a contributor has solved an issue.
    - One row per (user, issue) pair.
    - Multiple users may solve the same issue.
    - Same user may NOT solve the same issue twice (DB-level unique constraint).
    - issue uses SET_NULL so that if a closed issue is eventually cleaned up
      after the retention window, the historical solved record is preserved
      and never causes a DB error (issue becomes NULL but solved_at/user remain).
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="solved_issues",
    )
    issue = models.ForeignKey(
        Issue,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="solved_by",
    )
    solved_at = models.DateTimeField(auto_now_add=True)
    # Only verified GitHub completions count toward ML history.
    # Legacy rows created by the old manual "Solved" button stay False.
    is_verified = models.BooleanField(default=False, db_index=True)
    github_pr_url = models.URLField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "issue"],
                name="unique_user_issue_solved",
            )
        ]
        ordering = ["-solved_at"]
        indexes = [
            models.Index(fields=["user", "solved_at"]),
            models.Index(fields=["issue"]),
        ]

    def __str__(self):
        return f"{self.user} solved {self.issue} at {self.solved_at}"
