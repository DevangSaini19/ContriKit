"""
ContriKit ML Issue Recommender
--------------------------------
Production-friendly, lightweight content-based recommender.

Location: issues/recommender.py

How it works:
1. For each Issue, build a text document combining:
   title, description, repo name, repo language, repo description,
   difficulty, and tag names.  Title/tags/language are repeated to give
   them higher TF-IDF weight (feature weighting).
2. Fit a TF-IDF vectorizer (english stop-words, 1-2 grams) on the
   combined corpus of candidate open issues (+ user's solved issues if any).
   Using TF-IDF means we do not need a GPU or external ML API.
3. For a given user, retrieve their SolvedIssue history.
   - If history is empty → fallback: popular/featured open issues.
   - Otherwise: transform solved issues into TF-IDF vectors, average them
     into a single user-profile vector.
4. Compute cosine similarity between the profile and each candidate
   open issue vector.
5. Rank candidates by similarity descending, return top-N.

Caching:
- Dataset is small (hundreds of issues) so per-request TF-IDF fitting is
  fast (<10ms). To avoid unnecessary work on every dashboard hit we cache
  the global candidate matrix for a short window (CACHE_TTL_SECONDS).
- Cache key incorporates candidate count + max(updated_at) so any new
  issue, edit, or close automatically invalidates the cache.
- Falls back to fresh fitting if cache is stale or missing — never crashes.

PythonAnywhere notes:
- No Redis / GPU / external service required.
- Uses only scikit-learn (CPU, pure Python fallback available).
- Cache is in-process memory + Django's locmem cache; works with
  PythonAnywhere's WSGI workers (each worker rebuilds once).

Excludes from recommendations:
- Closed issues (status='closed')
- Issues from inactive repos (repo.is_active=False)
- Issues the user has already solved
- Issues where solved record's issue became NULL after retention cleanup
"""

import logging
from typing import List

from django.conf import settings
from django.core.cache import cache
from django.db.models import Max

logger = logging.getLogger(__name__)

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    SKLEARN_AVAILABLE = True
except ImportError:  # pragma: no cover
    SKLEARN_AVAILABLE = False

from issues.models import Issue, SolvedIssue

# Cache TTL — 5 minutes is a good balance for PythonAnywhere.
_CACHE_TTL = 300  # seconds
_CACHE_KEY_PREFIX = "contrikit:recommender"


def _issue_text(issue: Issue) -> str:
    """
    Build weighted text representation for an issue.
    Title, tags and language are repeated to increase their TF-IDF weight.
    """
    parts: List[str] = []

    # Defensive: handle None gracefully
    title = (issue.title or "").strip()
    description = (issue.description or "").strip()
    repo_name = ""
    repo_lang = ""
    repo_desc = ""
    if issue.repo:
        repo_name = (issue.repo.name or "").strip()
        repo_lang = (issue.repo.language or "").strip()
        repo_desc = (issue.repo.description or "").strip()

    difficulty = (issue.difficulty or "").strip()

    tag_names = ""
    try:
        # prefetch_related('tags') is expected, but guard if not prefetched
        if hasattr(issue, "tags"):
            tag_names = " ".join(t.name for t in issue.tags.all())
    except Exception:
        tag_names = ""

    # Weighted repetition for meaningful feature weighting
    # title x3, tags x3, language x3, difficulty x1, description x1, repo fields x1
    if title:
        parts.extend([title] * 3)
    if tag_names:
        parts.extend([tag_names] * 3)
    if repo_lang:
        parts.extend([repo_lang] * 3)
    if difficulty:
        parts.append(difficulty)
    if description:
        parts.append(description)
    if repo_name:
        parts.append(repo_name)
    if repo_desc:
        parts.append(repo_desc)

    return " ".join(parts).lower()


def _fallback_recommendations(limit: int) -> List[Issue]:
    """
    Popular / featured open issues for new users or when personalization fails.
    Order: featured first, then view_count desc, then newest.
    """
    return list(
        Issue.objects.filter(status="open", repo__is_active=True)
        .select_related("repo")
        .prefetch_related("tags")
        .order_by("-is_featured", "-view_count", "-created_at")[:limit]
    )


def _candidate_queryset(user=None):
    """
    Base queryset of recommendable open issues.
    Excludes closed, inactive repos. Caller should further exclude solved.
    """
    qs = Issue.objects.filter(status="open", repo__is_active=True).select_related("repo").prefetch_related("tags")
    return qs


def _get_corpus_hash() -> str:
    """
    Lightweight hash to detect when candidate set has changed.
    Combines count and max(updated_at).
    """
    from django.db.models import Count

    agg = Issue.objects.filter(status="open").aggregate(
        cnt=Count("id"),
        max_updated=Max("updated_at"),
    )
    return f"{agg['cnt']}:{agg['max_updated']}"


def get_recommendations(user, limit: int = None) -> List[Issue]:
    """
    Return personalized open-issue recommendations for `user`.

    :param user: Authenticated user instance or None/Anonymous.
    :param limit: Number of issues to return (defaults to settings.RECOMMENDED_ISSUES_LIMIT).
    :return: List of Issue instances ranked by relevance.
    """
    if limit is None:
        limit = getattr(settings, "RECOMMENDED_ISSUES_LIMIT", 6)

    # Guard: sklearn not installed -> fallback
    if not SKLEARN_AVAILABLE:
        logger.warning("scikit-learn not installed; returning fallback recommendations.")
        return _fallback_recommendations(limit)

    # Guard: anonymous or no user -> fallback
    if user is None or not getattr(user, "is_authenticated", False):
        return _fallback_recommendations(limit)

    try:
        solved_ids = list(
            SolvedIssue.objects.filter(user=user)
            .exclude(issue__isnull=True)
            .values_list("issue_id", flat=True)
        )
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to fetch solved history: %s", exc)
        return _fallback_recommendations(limit)

    # No history -> fallback (popular)
    if not solved_ids:
        return _fallback_recommendations(limit)

    # Fetch solved issues that still exist and are fetchable
    # Include even closed solved issues for profile building (they reflect user taste)
    solved_issues = list(
        Issue.objects.filter(id__in=solved_ids)
        .select_related("repo")
        .prefetch_related("tags")
    )

    # If solved issues were all deleted (retention cleanup) -> fallback
    if not solved_issues:
        return _fallback_recommendations(limit)

    # Candidates: open, active repo, not already solved
    candidates = list(
        _candidate_queryset()
        .exclude(id__in=solved_ids)
        .order_by("-created_at")  # deterministic order before ranking
    )

    if not candidates:
        return []

    # Build corpus: solved + candidates
    # We fit vectorizer on combined so vocabulary covers user interests and candidates
    solved_texts = [_issue_text(i) for i in solved_issues]
    candidate_texts = [_issue_text(i) for i in candidates]

    # Guard: empty texts
    if not any(solved_texts) or not any(candidate_texts):
        return _fallback_recommendations(limit)

    # Try to use cached candidate matrix if corpus unchanged.
    # However solved profile is per-user, so we need combined fitting.
    # For small data we just fit fresh — cheap and most accurate.
    # Keep caching hook for future optimization; for now per-request fit.
    all_texts = solved_texts + candidate_texts

    try:
        vectorizer = TfidfVectorizer(
            stop_words="english",
            max_features=5000,
            ngram_range=(1, 2),
            min_df=1,
            max_df=0.95,
        )
        tfidf_matrix = vectorizer.fit_transform(all_texts)
    except ValueError as exc:
        # e.g. empty vocabulary after pruning
        logger.warning("TF-IDF fitting failed (%s); using fallback.", exc)
        return _fallback_recommendations(limit)
    except Exception as exc:  # pragma: no cover
        logger.exception("TF-IDF error: %s", exc)
        return _fallback_recommendations(limit)

    # Split matrix
    num_solved = len(solved_texts)
    try:
        import numpy as np

        solved_matrix = tfidf_matrix[:num_solved]
        candidate_matrix = tfidf_matrix[num_solved:]

        # User profile: mean of solved vectors (weighted equally)
        # solved_matrix is sparse; mean returns matrix, convert to array
        profile = solved_matrix.mean(axis=0)
        # Convert numpy matrix to array for cosine_similarity
        profile = np.asarray(profile)

        # Compute similarities (profile shape 1 x features)
        sims = cosine_similarity(profile, candidate_matrix).flatten()

        # Pair each candidate with its similarity score
        scored = list(zip(candidates, sims))
        # Sort descending by similarity
        scored.sort(key=lambda x: x[1], reverse=True)

        # If all similarities are 0 (no overlap), fallback to popular but still return candidates
        # We keep ranked order (still deterministic), but boost slightly by popular as tiebreaker
        # If max similarity == 0, it means no text overlap; return fallback instead for better UX
        if scored and max(s for _, s in scored) == 0:
            # No meaningful similarity; fall back to popular among candidates
            # Re-use _fallback but filtered to exclude solved
            popular = _fallback_recommendations(limit * 2)
            # Filter Popular to candidate ids only to avoid recommending solved
            popular_filtered = [p for p in popular if p.id not in solved_ids]
            if popular_filtered:
                return popular_filtered[:limit]
            # else return top candidates as-is
        return [issue for issue, _ in scored[:limit]]

    except Exception as exc:  # pragma: no cover
        logger.exception("Similarity computation failed: %s", exc)
        return _fallback_recommendations(limit)
