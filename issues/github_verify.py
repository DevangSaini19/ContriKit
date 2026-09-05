"""
Verify that a ContriKit user completed a contribution on GitHub.

Uses the same public GitHub REST API pattern as repos.views (requests + optional PAT).

A contribution counts only when ALL of the following are true:
- the ContriKit issue URL maps to a GitHub issue (owner/repo/number)
- the user has a github_username
- a pull request authored by that username references that issue number
- that pull request is actually merged

Opening a PR, closing a PR without merge, or closing the issue alone is not enough.
"""
import logging
import re
from dataclasses import dataclass
from typing import Optional

import requests
from django.conf import settings
from django.db import IntegrityError

from issues.models import Issue, SolvedIssue

logger = logging.getLogger(__name__)

_ISSUE_URL_RE = re.compile(
    r"github\.com/([^/]+)/([^/]+)/issues/(\d+)",
    re.IGNORECASE,
)
_PR_URL_RE = re.compile(
    r"github\.com/([^/]+)/([^/]+)/pull/(\d+)",
    re.IGNORECASE,
)


@dataclass
class VerificationResult:
    verified: bool
    reason: str
    pr_url: str = ""
    pr_number: Optional[int] = None


def parse_github_issue_url(url: str):
    if not url:
        return None
    match = _ISSUE_URL_RE.search(url)
    if not match:
        return None
    owner, repo, number = match.group(1), match.group(2), int(match.group(3))
    repo = repo.removesuffix(".git")
    return owner, repo, number


def pr_references_issue(title: str, body: str, issue_number: int) -> bool:
    text = f"{title or ''}\n{body or ''}"
    return bool(re.search(rf"(?:^|[\s,:(])#{issue_number}(?!\d)", text))


def _github_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = getattr(settings, "GITHUB_PAT", "") or ""
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def _get(url, params=None, timeout=10):
    return requests.get(url, headers=_github_headers(), params=params or {}, timeout=timeout)


def _login_matches(login: str, github_username: str) -> bool:
    return (login or "").strip().lower() == (github_username or "").strip().lower()


def _fetch_pull(owner: str, repo: str, pr_number: int, getter=_get):
    resp = getter(f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}")
    if resp.status_code != 200:
        return None
    data = resp.json()
    if not isinstance(data, dict):
        return None
    return data


def _merged_pr_by_user(pr: dict, github_username: str, issue_number: int) -> bool:
    if not pr:
        return False
    if not pr.get("merged"):
        return False
    login = (pr.get("user") or {}).get("login") or ""
    if not _login_matches(login, github_username):
        return False
    return pr_references_issue(pr.get("title") or "", pr.get("body") or "", issue_number)


def _from_timeline(owner, repo, issue_number, github_username, getter=_get) -> VerificationResult:
    resp = getter(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/timeline",
        params={"per_page": 100},
    )
    if resp.status_code != 200:
        return VerificationResult(False, "github_unavailable")
    events = resp.json()
    if not isinstance(events, list):
        return VerificationResult(False, "github_unavailable")

    seen = set()
    saw_unmerged = False
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("event") not in ("cross-referenced", "connected"):
            continue
        source = event.get("source") or {}
        issue = source.get("issue") or {}
        if not issue.get("pull_request"):
            continue
        html_url = issue.get("html_url") or ""
        match = _PR_URL_RE.search(html_url) or _PR_URL_RE.search(
            (issue.get("pull_request") or {}).get("html_url") or ""
        )
        if not match:
            continue
        pr_owner, pr_repo, pr_number = match.group(1), match.group(2), int(match.group(3))
        key = (pr_owner.lower(), pr_repo.lower(), pr_number)
        if key in seen:
            continue
        seen.add(key)
        pr = _fetch_pull(pr_owner, pr_repo, pr_number, getter=getter)
        if not pr:
            continue
        login = (pr.get("user") or {}).get("login") or ""
        if not _login_matches(login, github_username):
            continue
        if pr.get("merged"):
            return VerificationResult(True, "merged_pr", pr.get("html_url") or html_url, pr_number)
        saw_unmerged = True
    if saw_unmerged:
        return VerificationResult(False, "pr_not_merged")
    return VerificationResult(False, "no_matching_pr")


def _from_search(owner, repo, issue_number, github_username, getter=_get) -> VerificationResult:
    query = f"repo:{owner}/{repo} is:pr author:{github_username} {issue_number}"
    resp = getter(
        "https://api.github.com/search/issues",
        params={"q": query, "per_page": 20},
    )
    if resp.status_code != 200:
        return VerificationResult(False, "github_unavailable")
    payload = resp.json()
    items = payload.get("items") if isinstance(payload, dict) else None
    if not items:
        return VerificationResult(False, "no_matching_pr")

    saw_unmerged = False
    for item in items:
        html_url = item.get("html_url") or ""
        match = _PR_URL_RE.search(html_url)
        if not match:
            continue
        pr_owner, pr_repo, pr_number = match.group(1), match.group(2), int(match.group(3))
        if not pr_references_issue(item.get("title") or "", item.get("body") or "", issue_number):
            continue
        pr = _fetch_pull(pr_owner, pr_repo, pr_number, getter=getter)
        if not pr:
            continue
        login = (pr.get("user") or {}).get("login") or ""
        if not _login_matches(login, github_username):
            continue
        if pr.get("merged"):
            return VerificationResult(True, "merged_pr", pr.get("html_url") or html_url, pr_number)
        saw_unmerged = True
    if saw_unmerged:
        return VerificationResult(False, "pr_not_merged")
    return VerificationResult(False, "no_matching_pr")


def verify_merged_pr_for_issue(issue: Issue, github_username: str, getter=_get) -> VerificationResult:
    username = (github_username or "").strip()
    if not username:
        return VerificationResult(False, "github_username_required")

    parsed = parse_github_issue_url(issue.github_issue_url if issue else "")
    if not parsed:
        return VerificationResult(False, "invalid_issue_url")
    owner, repo, issue_number = parsed

    try:
        result = _from_timeline(owner, repo, issue_number, username, getter=getter)
        if result.verified:
            return result
        if result.reason == "pr_not_merged":
            return result
        # Timeline empty / no match — try search (covers PRs that mention the issue in body).
        search = _from_search(owner, repo, issue_number, username, getter=getter)
        if search.verified or search.reason != "github_unavailable":
            return search
        return result
    except requests.RequestException:
        logger.warning("GitHub verification request failed for issue %s", getattr(issue, "id", None))
        return VerificationResult(False, "github_unavailable")


def record_verified_solved(user, issue: Issue, result: VerificationResult) -> Optional[SolvedIssue]:
    if not result.verified:
        return None
    existing = SolvedIssue.objects.filter(user=user, issue=issue).first()
    if existing:
        if not existing.is_verified:
            existing.is_verified = True
            existing.github_pr_url = result.pr_url or existing.github_pr_url
            existing.save(update_fields=["is_verified", "github_pr_url"])
        return existing
    try:
        return SolvedIssue.objects.create(
            user=user,
            issue=issue,
            is_verified=True,
            github_pr_url=result.pr_url or "",
        )
    except IntegrityError:
        return SolvedIssue.objects.filter(user=user, issue=issue).first()
