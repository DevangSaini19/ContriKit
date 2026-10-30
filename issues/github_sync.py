"""
Synchronize a ContriKit Issue's open/closed status with GitHub.

This module is the single source of truth for GitHub status synchronization.
It is intentionally independent from ``issues.github_verify`` (which decides
whether a *contributor* solved an issue): this one only mirrors the state of
the linked GitHub issue/pull request.

Design rules:

* Only ``open`` and ``closed`` are valid states. An unexpected value is a
  failure, never silently coerced to ``open``.
* Every unsuccessful API request (auth, rate limit, 404, 5xx, timeout, bad
  JSON, missing fields) is a *failure*, never evidence that the issue is open
  or closed. Failures never touch the database.
* A closed issue stores GitHub's own ``closed_at`` timestamp. We never
  substitute ``timezone.now()`` for it.
* Writes are idempotent: a row whose status and timestamp already match is
  left untouched (no ``UPDATE``).
* Rows flagged ``is_demo`` are skipped: their URLs are synthetic and must not
  be synchronized against real GitHub data.

No third-party dependency beyond ``requests`` (already used by the project).
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from issues.models import Issue

logger = logging.getLogger(__name__)

GITHUB_API_ROOT = "https://api.github.com"
DEFAULT_TIMEOUT = 10
USER_AGENT = "ContriKit-IssueStatusSync/1.0"

VALID_STATES = ("open", "closed")

# Reasons an individual issue could not be synchronized. These are stable
# identifiers so the management command and tests can group them.
REASON_INVALID_URL = "invalid_url"
REASON_DEMO = "demo_issue"
REASON_UNAUTHORIZED = "unauthorized"
REASON_FORBIDDEN = "forbidden"
REASON_RATE_LIMITED = "rate_limited"
REASON_NOT_FOUND = "not_found"
REASON_SERVER_ERROR = "server_error"
REASON_NETWORK_ERROR = "network_error"
REASON_TIMEOUT = "timeout"
REASON_INVALID_JSON = "invalid_json"
REASON_MALFORMED = "malformed_response"
REASON_MISSING_STATE = "missing_state"
REASON_INVALID_STATE = "invalid_state"
REASON_INVALID_TIMESTAMP = "invalid_timestamp"

FAILURE_REASONS = frozenset(
    {
        REASON_INVALID_URL,
        REASON_UNAUTHORIZED,
        REASON_FORBIDDEN,
        REASON_RATE_LIMITED,
        REASON_NOT_FOUND,
        REASON_SERVER_ERROR,
        REASON_NETWORK_ERROR,
        REASON_TIMEOUT,
        REASON_INVALID_JSON,
        REASON_MALFORMED,
        REASON_MISSING_STATE,
        REASON_INVALID_STATE,
        REASON_INVALID_TIMESTAMP,
    }
)

# owner/repo/kind/number. Accepts /issues/N and /pull/N, optional trailing
# slash, query string and fragment. Only github.com is supported.
_GITHUB_ITEM_RE = re.compile(
    r"^/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"/(?P<kind>issues|pull)/(?P<number>\d+)/?$"
)


@dataclass(frozen=True)
class GithubRef:
    owner: str
    repo: str
    number: int
    kind: str

    @property
    def slug(self):
        return f"{self.owner}/{self.repo}"


@dataclass
class FetchResult:
    """Outcome of one GitHub API request."""

    ok: bool
    reason: str = ""
    status_code: Optional[int] = None
    state: Optional[str] = None
    closed_at: Optional[object] = None
    closed_at_raw: Optional[str] = None
    title: str = ""
    html_url: str = ""
    message: str = ""


@dataclass
class SyncResult:
    """Outcome of syncing one local Issue."""

    # "updated" | "unchanged" | "would_update" | "skipped" | "failed"
    action: str
    reason: str = ""
    changes: dict = field(default_factory=dict)
    message: str = ""

    @property
    def changed(self):
        return self.action in ("updated", "would_update")

    @property
    def failed(self):
        return self.action == "failed"


def parse_github_item_url(url):
    """Parse a GitHub issue or pull request URL.

    Returns a :class:`GithubRef` for a valid ``https://github.com/owner/repo``
    ``/issues/N`` or ``/pull/N`` URL, otherwise ``None``. Rejects other hosts,
    malformed paths and non-numeric numbers.
    """
    if not url or not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if host not in ("github.com", "www.github.com"):
        return None
    match = _GITHUB_ITEM_RE.match(parsed.path or "")
    if not match:
        return None
    try:
        number = int(match.group("number"))
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return GithubRef(
        owner=match.group("owner"),
        repo=match.group("repo"),
        number=number,
        kind=match.group("kind"),
    )


def _github_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    token = (getattr(settings, "GITHUB_PAT", "") or "").strip()
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def _default_get(url, params=None, timeout=DEFAULT_TIMEOUT):
    return requests.get(
        url, headers=_github_headers(), params=params or {}, timeout=timeout
    )


def _is_rate_limited(response):
    if response.status_code == 429:
        return True
    if response.status_code == 403:
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining == "0":
            return True
    return False


def _headers_of(response):
    return getattr(response, "headers", {}) or {}


def _failure_reason_for_status(response):
    code = response.status_code
    if code in (401,):
        return REASON_UNAUTHORIZED
    if code == 403:
        return REASON_RATE_LIMITED if _is_rate_limited(response) else REASON_FORBIDDEN
    if code == 404:
        return REASON_NOT_FOUND
    if code == 429:
        return REASON_RATE_LIMITED
    if 400 <= code < 500:
        # Unprocessable / gone / other client errors: still never "open".
        return REASON_MALFORMED
    return REASON_SERVER_ERROR


def _coerce_closed_at(raw):
    """Parse GitHub's ISO-8601 closed_at into an aware datetime.

    Returns ``(datetime_or_None, reason)``. ``reason`` is empty on success or
    when there is simply nothing to parse (issue open / closed_at null).
    """
    if raw in (None, ""):
        return None, ""
    if not isinstance(raw, str):
        return None, REASON_INVALID_TIMESTAMP
    parsed = parse_datetime(raw)
    if parsed is None:
        return None, REASON_INVALID_TIMESTAMP
    if timezone.is_naive(parsed):
        try:
            parsed = timezone.make_aware(parsed, timezone.utc)
        except Exception:  # pragma: no cover - defensive
            return None, REASON_INVALID_TIMESTAMP
    return parsed, ""


def fetch_github_item(ref, getter=None, timeout=DEFAULT_TIMEOUT):
    """Fetch one issue/PR from the GitHub Issues API and validate the payload.

    The Issues API is used for both issues and pull requests; PRs carry a
    ``pull_request`` key but the same ``state`` / ``closed_at`` fields.
    """
    getter = getter or _default_get
    url = f"{GITHUB_API_ROOT}/repos/{ref.owner}/{ref.repo}/issues/{ref.number}"

    try:
        response = getter(url, timeout=timeout)
    except requests.Timeout:
        return FetchResult(False, REASON_TIMEOUT, message="GitHub request timed out")
    except requests.RequestException as exc:
        logger.warning("GitHub request failed for %s: %s", ref, type(exc).__name__)
        return FetchResult(False, REASON_NETWORK_ERROR, message=str(exc))

    status_code = getattr(response, "status_code", None)
    if status_code != 200:
        reason = _failure_reason_for_status(response)
        logger.info(
            "GitHub returned %s (%s) for %s", status_code, reason, ref
        )
        return FetchResult(
            False,
            reason,
            status_code=status_code,
            message=f"GitHub returned HTTP {status_code}",
        )

    try:
        payload = response.json()
    except ValueError:
        return FetchResult(False, REASON_INVALID_JSON, status_code=200)
    if not isinstance(payload, dict):
        return FetchResult(False, REASON_MALFORMED, status_code=200)

    state = payload.get("state")
    if state is None:
        return FetchResult(False, REASON_MISSING_STATE, status_code=200)
    if not isinstance(state, str) or state.lower() not in VALID_STATES:
        return FetchResult(
            False, REASON_INVALID_STATE, status_code=200, state=str(state)
        )
    state = state.lower()

    raw_closed_at = payload.get("closed_at")
    closed_at, ts_reason = _coerce_closed_at(raw_closed_at)
    if ts_reason:
        # The state is trustworthy but the timestamp is not. Treat it as a
        # failure so a bad timestamp can never be written to the database.
        return FetchResult(
            False,
            REASON_INVALID_TIMESTAMP,
            status_code=200,
            state=state,
            closed_at_raw=raw_closed_at if isinstance(raw_closed_at, str) else None,
        )

    return FetchResult(
        True,
        status_code=200,
        state=state,
        closed_at=closed_at,
        closed_at_raw=raw_closed_at,
        title=(payload.get("title") or "")[:300],
        html_url=payload.get("html_url") or "",
    )


def _plan_changes(issue, fetched):
    """Compute the minimal set of field changes needed. Pure function."""
    changes = {}
    if issue.status != fetched.state:
        changes["status"] = fetched.state

    if fetched.state == "closed":
        github_closed_at = fetched.closed_at
        if github_closed_at is not None and issue.closed_at != github_closed_at:
            changes["closed_at"] = github_closed_at
    else:
        # Open on GitHub: any stale local closed_at is wrong.
        if issue.closed_at is not None:
            changes["closed_at"] = None

    return changes


def sync_issue(issue, getter=None, dry_run=False, timeout=DEFAULT_TIMEOUT):
    """Synchronize one Issue with GitHub.

    Returns a :class:`SyncResult`. When ``dry_run`` is set, nothing is written
    and the action is ``would_update`` / ``unchanged`` / ``skipped`` / ``failed``.
    """
    if issue.is_demo:
        return SyncResult(
            "skipped", REASON_DEMO, message="Demo issue excluded from GitHub sync"
        )

    ref = parse_github_item_url(issue.github_issue_url)
    if ref is None:
        return SyncResult(
            "failed",
            REASON_INVALID_URL,
            message=f"Not a valid GitHub issue/PR URL: {issue.github_issue_url!r}",
        )

    fetched = fetch_github_item(ref, getter=getter, timeout=timeout)
    if not fetched.ok:
        return SyncResult("failed", fetched.reason, message=fetched.message)

    changes = _plan_changes(issue, fetched)

    if not changes:
        # Status and timestamp already correct; only record the successful
        # check (single cheap write, no status/timestamp mutation).
        if not dry_run and issue.github_synced_at is None:
            Issue.objects.filter(pk=issue.pk).update(
                github_synced_at=timezone.now()
            )
        return SyncResult("unchanged", message="Already up to date")

    if dry_run:
        return SyncResult("would_update", changes=changes)

    updates = dict(changes)
    updates["github_synced_at"] = timezone.now()
    updates["updated_at"] = timezone.now()
    with transaction.atomic():
        # Bypass Issue.save(): the model stamps closed_at=now() when a closed
        # row has no timestamp, which would overwrite GitHub's real value.
        Issue.objects.filter(pk=issue.pk).update(**updates)
    for name, value in changes.items():
        setattr(issue, name, value)
    issue.github_synced_at = updates["github_synced_at"]
    return SyncResult("updated", changes=changes)
