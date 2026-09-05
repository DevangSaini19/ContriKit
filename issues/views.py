from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST
from .models import Issue, Tag, SavedIssue, SolvedIssue
from .github_verify import record_verified_solved, verify_merged_pr_for_issue
from .recommender import get_recommendations
from templates_app.models import Template

_VERIFY_MESSAGES = {
    'github_username_required': 'Add your GitHub username to your account so we can match your merged pull requests.',
    'invalid_issue_url': 'This issue is not linked to a GitHub issue URL we can verify.',
    'github_unavailable': 'GitHub could not be reached. Try again in a moment.',
    'pr_not_merged': 'A pull request was found but it is not merged yet.',
    'no_matching_pr': 'No merged pull request by your GitHub account was found for this issue.',
    'verification_required': 'Solved history is created only after a merged GitHub pull request is verified.',
}


def _verified_solved_ids(user):
    return set(
        SolvedIssue.objects.filter(user=user, is_verified=True)
        .exclude(issue__isnull=True)
        .values_list('issue_id', flat=True)
    )


def _maybe_auto_verify(request, issue):
    """Check GitHub on issue view so solved state can appear without a claim button."""
    user = request.user
    if not user.is_authenticated:
        return
    if SolvedIssue.objects.filter(user=user, issue=issue, is_verified=True).exists():
        return
    if not (user.github_username or '').strip():
        return
    session_key = f'gh_verify_{issue.id}'
    last = request.session.get(session_key)
    now = timezone.now().timestamp()
    if last and (now - float(last)) < 120:
        return
    request.session[session_key] = now
    result = verify_merged_pr_for_issue(issue, user.github_username)
    if result.verified:
        record_verified_solved(user, issue, result)


def issue_list_view(request):
    query = request.GET.get('q', '').strip()
    language = request.GET.get('lang', '').strip()
    difficulty = request.GET.get('diff', '').strip()
    tag_slug = request.GET.get('tag', '').strip()

    issues = Issue.objects.filter(status='open', repo__is_active=True).select_related('repo').prefetch_related('tags').order_by('-created_at')

    if query:
        issues = issues.filter(Q(title__icontains=query) | Q(description__icontains=query) | Q(repo__name__icontains=query))
    if language:
        issues = issues.filter(repo__language__iexact=language)
    if difficulty:
        issues = issues.filter(difficulty=difficulty)
    if tag_slug:
        issues = issues.filter(tags__slug=tag_slug)

    # Distinct languages for filter dropdown
    languages = Issue.objects.filter(status='open', repo__is_active=True).exclude(repo__language='').values_list('repo__language', flat=True).distinct()
    tags = Tag.objects.all()

    paginator = Paginator(issues, 12)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Saved issue IDs for current user to show saved state
    saved_ids = set()
    solved_ids = set()
    if request.user.is_authenticated:
        saved_ids = set(SavedIssue.objects.filter(user=request.user).values_list('issue_id', flat=True))
        solved_ids = _verified_solved_ids(request.user)

    context = {
        'page_obj': page_obj,
        'languages': sorted(set(languages)),
        'tags': tags,
        'q': query,
        'selected_lang': language,
        'selected_diff': difficulty,
        'selected_tag': tag_slug,
        'saved_ids': saved_ids,
        'solved_ids': solved_ids,
    }
    return render(request, 'issues/issue_list.html', context)

def issue_detail_view(request, id):
    issue = get_object_or_404(Issue.objects.select_related('repo', 'posted_by').prefetch_related('tags'), id=id)

    # View count increment once per session per issue
    session_key = f"viewed_issue_{issue.id}"
    if not request.session.get(session_key, False):
        issue.view_count += 1
        issue.save(update_fields=['view_count'])
        request.session[session_key] = True

    is_saved = False
    is_solved = False
    if request.user.is_authenticated:
        _maybe_auto_verify(request, issue)
        is_saved = SavedIssue.objects.filter(user=request.user, issue=issue).exists()
        is_solved = SolvedIssue.objects.filter(user=request.user, issue=issue, is_verified=True).exists()

    # Get template files to display
    templates = Template.objects.all()

    return render(request, 'issues/issue_detail.html', {
        'issue': issue,
        'is_saved': is_saved,
        'is_solved': is_solved,
        'templates': templates,
    })

@require_POST
def toggle_save_view(request, id):
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'login_required'}, status=403)

    issue = get_object_or_404(Issue, id=id)
    saved_obj = SavedIssue.objects.filter(user=request.user, issue=issue).first()

    if saved_obj:
        saved_obj.delete()
        return JsonResponse({'status': 'unsaved', 'issue_id': issue.id})
    else:
        SavedIssue.objects.create(user=request.user, issue=issue)
        return JsonResponse({'status': 'saved', 'issue_id': issue.id})

@require_POST
def mark_solved_view(request, id):
    """Manual claims are rejected. Solved records come only from GitHub verification."""
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'login_required'}, status=403)
    return JsonResponse(
        {'error': 'verification_required', 'message': _VERIFY_MESSAGES['verification_required']},
        status=403,
    )


@require_POST
def verify_contribution_view(request, id):
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'login_required'}, status=403)

    issue = get_object_or_404(Issue, id=id)
    result = verify_merged_pr_for_issue(issue, request.user.github_username)
    if result.verified:
        obj = record_verified_solved(request.user, issue, result)
        return JsonResponse({
            'status': 'solved',
            'issue_id': issue.id,
            'pr_url': result.pr_url,
            'solved_at': obj.solved_at.isoformat() if obj else None,
        })
    return JsonResponse(
        {'error': result.reason, 'message': _VERIFY_MESSAGES.get(result.reason, result.reason)},
        status=400,
    )


@login_required
def recommended_issues_view(request):
    try:
        recommended_issues = get_recommendations(request.user)
    except Exception:
        recommended_issues = []
    solved_count = SolvedIssue.objects.filter(user=request.user, is_verified=True).exclude(issue__isnull=True).count()
    return render(request, 'issues/recommended.html', {
        'recommended_issues': recommended_issues,
        'solved_ids': _verified_solved_ids(request.user),
        'has_solved_history': solved_count > 0,
        'solved_count': solved_count,
    })
