from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from issues.github_verify import VerificationResult, pr_references_issue, verify_merged_pr_for_issue
from issues.models import Issue, SolvedIssue, Tag
from issues.recommender import SKLEARN_AVAILABLE, _issue_text, get_recommendations
from repos.models import Repo

User = get_user_model()


class FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def github_getter(*, timeline=None, pulls=None, search=None, timeline_status=200, search_status=200):
    pulls = pulls or {}
    search = search if search is not None else []

    def getter(url, params=None, **kwargs):
        if '/timeline' in url:
            return FakeResp(timeline_status, timeline if timeline is not None else [])
        if '/search/issues' in url:
            return FakeResp(search_status, {'items': search})
        if '/pulls/' in url:
            number = int(url.rstrip('/').split('/')[-1])
            pr = pulls.get(number)
            if pr is None:
                return FakeResp(404, {})
            return FakeResp(200, pr)
        return FakeResp(404, {})

    return getter


class SolvedMLFixturesMixin:
    def make_user(self, username, role='viewer', github_username=''):
        return User.objects.create_user(
            username=username,
            email=f'{username}@example.com',
            password='pass12345',
            role=role,
            github_username=github_username,
        )

    def make_repo(self, editor, name, language='Python', description='', is_active=True):
        return Repo.objects.create(
            editor=editor,
            github_url=f'https://github.com/example/{name}',
            name=name,
            language=language,
            description=description or f'{name} repository',
            is_active=is_active,
        )

    def make_issue(self, repo, posted_by, title, description='desc', difficulty='beginner', status='open', tags=None, **kwargs):
        n = Issue.objects.count() + 1
        issue = Issue.objects.create(
            repo=repo,
            posted_by=posted_by,
            title=title,
            description=description,
            github_issue_url=f'https://github.com/example/{repo.name}/issues/{n}',
            difficulty=difficulty,
            estimated_hours=2,
            status=status,
            **kwargs,
        )
        if tags:
            issue.tags.set(tags)
        return issue

    def verified_solve(self, user, issue, pr_url='https://github.com/example/kit/pull/9'):
        return SolvedIssue.objects.create(
            user=user,
            issue=issue,
            is_verified=True,
            github_pr_url=pr_url,
        )


class MarkSolvedViewTests(SolvedMLFixturesMixin, TestCase):
    def setUp(self):
        self.editor = self.make_user('editor1', role='editor')
        self.user_a = self.make_user('usera', github_username='alice')
        self.user_b = self.make_user('userb', github_username='bob')
        self.repo = self.make_repo(self.editor, 'kit')
        self.issue = self.make_issue(self.repo, self.editor, 'Fix login bug')
        self.closed = self.make_issue(self.repo, self.editor, 'Closed bug', status='closed')
        self.url = reverse('mark_solved', args=[self.issue.id])
        self.verify_url = reverse('verify_contribution', args=[self.issue.id])
        self.closed_url = reverse('mark_solved', args=[self.closed.id])
        self.verify_patcher = patch(
            'issues.views.verify_merged_pr_for_issue',
            return_value=VerificationResult(False, 'no_matching_pr'),
        )
        self.verify_patcher.start()
        self.addCleanup(self.verify_patcher.stop)

    def authenticated_csrf_client(self, user):
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        response = client.get('/issues/')
        self.assertEqual(response.status_code, 200)
        return client, client.cookies['csrftoken'].value

    def test_no_manual_solved_button(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse('issue_detail', args=[self.issue.id]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'btn-mark-solved')
        self.assertContains(response, 'Contribute on GitHub')
        self.assertContains(response, 'btn-verify-contribution')

    def test_manual_post_cannot_create_solved_record(self):
        client, token = self.authenticated_csrf_client(self.user_a)
        response = client.post(self.url, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error'], 'verification_required')
        self.assertFalse(SolvedIssue.objects.filter(user=self.user_a, issue=self.issue).exists())

    def test_csrf_required_on_verify(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user_a)
        response = client.post(self.verify_url)
        self.assertEqual(response.status_code, 403)

    def test_unauthenticated_verify_rejected(self):
        client = Client(enforce_csrf_checks=True)
        login_page = client.get(reverse('login'))
        token = client.cookies['csrftoken'].value
        response = client.post(self.verify_url, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error'], 'login_required')

    def test_verified_contribution_creates_history(self):
        client, token = self.authenticated_csrf_client(self.user_a)
        with patch(
            'issues.views.verify_merged_pr_for_issue',
            return_value=VerificationResult(True, 'merged_pr', 'https://github.com/example/kit/pull/9'),
        ):
            response = client.post(self.verify_url, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        obj = SolvedIssue.objects.get(user=self.user_a, issue=self.issue)
        self.assertTrue(obj.is_verified)
        self.assertEqual(obj.github_pr_url, 'https://github.com/example/kit/pull/9')
        self.assertIsNotNone(obj.solved_at)

    def test_duplicate_verified_solve_is_idempotent(self):
        self.verified_solve(self.user_a, self.issue)
        client, token = self.authenticated_csrf_client(self.user_a)
        with patch(
            'issues.views.verify_merged_pr_for_issue',
            return_value=VerificationResult(True, 'merged_pr', 'https://github.com/example/kit/pull/9'),
        ):
            response = client.post(self.verify_url, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(SolvedIssue.objects.filter(user=self.user_a, issue=self.issue).count(), 1)

    def test_multiple_users_can_verify_same_issue(self):
        client_a, token_a = self.authenticated_csrf_client(self.user_a)
        client_b, token_b = self.authenticated_csrf_client(self.user_b)
        result = VerificationResult(True, 'merged_pr', 'https://github.com/example/kit/pull/9')
        with patch('issues.views.verify_merged_pr_for_issue', return_value=result):
            self.assertEqual(client_a.post(self.verify_url, HTTP_X_CSRFTOKEN=token_a).status_code, 200)
            self.assertEqual(client_b.post(self.verify_url, HTTP_X_CSRFTOKEN=token_b).status_code, 200)
        self.assertEqual(SolvedIssue.objects.filter(issue=self.issue, is_verified=True).count(), 2)

    def test_db_unique_constraint_prevents_duplicate(self):
        SolvedIssue.objects.create(user=self.user_a, issue=self.issue, is_verified=True)
        with self.assertRaises(IntegrityError):
            SolvedIssue.objects.create(user=self.user_a, issue=self.issue, is_verified=True)

    def test_closed_issue_hides_contribute_box_unless_verified(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse('issue_detail', args=[self.closed.id]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'btn-mark-solved')
        self.assertContains(response, 'This issue is closed')

    def test_closed_issues_excluded_from_listings(self):
        self.client.force_login(self.user_a)
        response = self.client.get('/issues/')
        self.assertContains(response, self.issue.title)
        self.assertNotContains(response, self.closed.title)

    def test_verified_history_preserved_when_issue_closed(self):
        self.verified_solve(self.user_a, self.issue)
        self.issue.status = 'closed'
        self.issue.save()
        rec = SolvedIssue.objects.get(user=self.user_a, issue=self.issue)
        self.assertTrue(rec.is_verified)
        self.assertIsNotNone(Issue.objects.get(pk=self.issue.pk).closed_at)

    def test_reopen_clears_closed_at(self):
        self.issue.status = 'closed'
        self.issue.save()
        self.issue.status = 'open'
        self.issue.save()
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, 'open')
        self.assertIsNone(self.issue.closed_at)

    def test_detail_shows_solved_after_verified(self):
        self.verified_solve(self.user_a, self.issue)
        self.client.force_login(self.user_a)
        response = self.client.get(reverse('issue_detail', args=[self.issue.id]))
        self.assertContains(response, 'Verified GitHub contribution')
        self.assertNotContains(response, 'btn-verify-contribution')


class GitHubVerificationLogicTests(SolvedMLFixturesMixin, TestCase):
    def setUp(self):
        self.editor = self.make_user('ed', role='editor')
        self.user = self.make_user('alice', github_username='alice')
        self.repo = self.make_repo(self.editor, 'kit')
        self.issue = self.make_issue(self.repo, self.editor, 'Fix login bug')

    def test_pr_references_issue_number(self):
        self.assertTrue(pr_references_issue('Fixes #1', '', 1))
        self.assertFalse(pr_references_issue('Fixes #12', '', 1))
        self.assertFalse(pr_references_issue('unrelated', 'no mention', 1))

    def test_merged_pr_on_timeline_verifies(self):
        getter = github_getter(
            timeline=[{
                'event': 'cross-referenced',
                'source': {
                    'issue': {
                        'html_url': 'https://github.com/example/kit/pull/9',
                        'pull_request': {'html_url': 'https://github.com/example/kit/pull/9'},
                    }
                },
            }],
            pulls={9: {
                'merged': True,
                'user': {'login': 'alice'},
                'html_url': 'https://github.com/example/kit/pull/9',
                'title': 'Fix',
                'body': 'Fixes #1',
            }},
        )
        result = verify_merged_pr_for_issue(self.issue, 'alice', getter=getter)
        self.assertTrue(result.verified)

    def test_unmerged_pr_does_not_count(self):
        getter = github_getter(
            timeline=[{
                'event': 'cross-referenced',
                'source': {
                    'issue': {
                        'html_url': 'https://github.com/example/kit/pull/9',
                        'pull_request': {'html_url': 'https://github.com/example/kit/pull/9'},
                    }
                },
            }],
            pulls={9: {
                'merged': False,
                'user': {'login': 'alice'},
                'html_url': 'https://github.com/example/kit/pull/9',
                'title': 'Fix',
                'body': 'Fixes #1',
            }},
        )
        result = verify_merged_pr_for_issue(self.issue, 'alice', getter=getter)
        self.assertFalse(result.verified)
        self.assertEqual(result.reason, 'pr_not_merged')

    def test_unrelated_pr_does_not_count(self):
        getter = github_getter(
            timeline=[],
            search=[{
                'html_url': 'https://github.com/example/kit/pull/9',
                'title': 'Other work',
                'body': 'Fixes #999',
            }],
            pulls={9: {
                'merged': True,
                'user': {'login': 'alice'},
                'html_url': 'https://github.com/example/kit/pull/9',
                'title': 'Other work',
                'body': 'Fixes #999',
            }},
        )
        result = verify_merged_pr_for_issue(self.issue, 'alice', getter=getter)
        self.assertFalse(result.verified)
        self.assertEqual(result.reason, 'no_matching_pr')

    def test_closed_issue_alone_does_not_count(self):
        self.issue.status = 'closed'
        self.issue.save()
        getter = github_getter(timeline=[], search=[])
        result = verify_merged_pr_for_issue(self.issue, 'alice', getter=getter)
        self.assertFalse(result.verified)

    def test_missing_username_does_not_count(self):
        result = verify_merged_pr_for_issue(self.issue, '', getter=github_getter())
        self.assertEqual(result.reason, 'github_username_required')


class CleanupClosedIssuesTests(SolvedMLFixturesMixin, TestCase):
    def setUp(self):
        self.editor = self.make_user('ed', role='editor')
        self.user = self.make_user('solver')
        self.repo = self.make_repo(self.editor, 'cleanup-repo')
        self.open_issue = self.make_issue(self.repo, self.editor, 'Still open')
        self.recent_closed = self.make_issue(self.repo, self.editor, 'Recently closed', status='closed')
        self.expired = self.make_issue(self.repo, self.editor, 'Expired closed', status='closed')
        Issue.objects.filter(pk=self.expired.pk).update(closed_at=timezone.now() - timedelta(days=40))
        self.expired.refresh_from_db()

    def test_dry_run_does_not_delete(self):
        out = StringIO()
        call_command('cleanup_closed_issues', '--dry-run', '--days', '30', stdout=out)
        self.assertTrue(Issue.objects.filter(pk=self.expired.pk).exists())
        self.assertIn('DRY RUN', out.getvalue())

    def test_only_expired_closed_removed_open_kept(self):
        call_command('cleanup_closed_issues', '--days', '30', stdout=StringIO())
        self.assertFalse(Issue.objects.filter(pk=self.expired.pk).exists())
        self.assertTrue(Issue.objects.filter(pk=self.open_issue.pk).exists())
        self.assertTrue(Issue.objects.filter(pk=self.recent_closed.pk).exists())

    def test_days_override(self):
        Issue.objects.filter(pk=self.recent_closed.pk).update(closed_at=timezone.now() - timedelta(days=5))
        call_command('cleanup_closed_issues', '--days', '3', stdout=StringIO())
        self.assertFalse(Issue.objects.filter(pk=self.recent_closed.pk).exists())
        self.assertTrue(Issue.objects.filter(pk=self.open_issue.pk).exists())

    def test_solved_history_safe_after_deletion(self):
        solved = self.verified_solve(self.user, self.expired)
        call_command('cleanup_closed_issues', '--days', '30', stdout=StringIO())
        solved.refresh_from_db()
        self.assertIsNone(solved.issue)
        self.assertTrue(solved.is_verified)
        self.assertEqual(solved.user_id, self.user.id)

    def test_two_deleted_issues_preserve_both_solved_rows(self):
        other = self.make_issue(self.repo, self.editor, 'Another expired', status='closed')
        Issue.objects.filter(pk=other.pk).update(closed_at=timezone.now() - timedelta(days=40))
        s1 = self.verified_solve(self.user, self.expired)
        s2 = self.verified_solve(self.user, other)
        call_command('cleanup_closed_issues', '--days', '30', stdout=StringIO())
        s1.refresh_from_db()
        s2.refresh_from_db()
        self.assertIsNone(s1.issue)
        self.assertIsNone(s2.issue)
        self.assertEqual(SolvedIssue.objects.filter(user=self.user, issue__isnull=True).count(), 2)


class RecommenderTests(SolvedMLFixturesMixin, TestCase):
    def setUp(self):
        self.editor = self.make_user('ed2', role='editor')
        self.user = self.make_user('learner')
        self.py_repo = self.make_repo(
            self.editor, 'django-app', language='Python',
            description='Django web framework backend APIs',
        )
        self.java_repo = self.make_repo(
            self.editor, 'spring-app', language='Java',
            description='Spring Boot enterprise Java services',
        )
        self.inactive_repo = self.make_repo(self.editor, 'dead-repo', language='Python', is_active=False)
        self.django_tag = Tag.objects.create(name='django', slug='django')
        self.java_tag = Tag.objects.create(name='java', slug='java')

        self.solved_py = self.make_issue(
            self.py_repo, self.editor,
            title='Fix Django ORM queryset bug',
            description='The Django ORM queryset is returning stale data from the cache.',
            tags=[self.django_tag],
        )
        self.open_py_1 = self.make_issue(
            self.py_repo, self.editor,
            title='Add Django REST serializer validation',
            description='Need Django serializers and queryset filtering for the API.',
            tags=[self.django_tag],
        )
        self.open_py_2 = self.make_issue(
            self.py_repo, self.editor,
            title='Django admin custom filter',
            description='Write a custom Django admin filter using QuerySet.',
            tags=[self.django_tag],
        )
        self.open_java = self.make_issue(
            self.java_repo, self.editor,
            title='Spring Boot JPA entity mapping',
            description='Map Hibernate entities in a Spring Boot microservice.',
            tags=[self.java_tag],
        )
        self.closed_py = self.make_issue(
            self.py_repo, self.editor,
            title='Closed Django migration issue',
            description='Django migrations already applied.',
            status='closed',
            tags=[self.django_tag],
        )
        self.inactive_issue = self.make_issue(
            self.inactive_repo, self.editor,
            title='Django on inactive repo',
            description='Django ORM work on an inactive repository.',
            tags=[self.django_tag],
        )
        self.sparse = self.make_issue(self.py_repo, self.editor, title='x', description='')

    def test_sklearn_available(self):
        self.assertTrue(SKLEARN_AVAILABLE)

    def test_unverified_history_uses_fallback(self):
        SolvedIssue.objects.create(user=self.user, issue=self.solved_py, is_verified=False)
        recs = get_recommendations(self.user, limit=6)
        ids = [i.id for i in recs]
        self.assertIn(self.solved_py.id, ids)

    def test_zero_solved_fallback_no_error(self):
        recs = get_recommendations(self.user, limit=6)
        self.assertGreater(len(recs), 0)
        ids = [i.id for i in recs]
        self.assertNotIn(self.closed_py.id, ids)
        self.assertNotIn(self.inactive_issue.id, ids)

    def test_one_verified_influences_recommendations(self):
        self.verified_solve(self.user, self.solved_py)
        recs = get_recommendations(self.user, limit=6)
        ids = [i.id for i in recs]
        self.assertNotIn(self.solved_py.id, ids)
        self.assertIn(self.open_py_1.id, ids)
        py_rank = ids.index(self.open_py_1.id)
        java_rank = ids.index(self.open_java.id) if self.open_java.id in ids else 99
        self.assertLess(py_rank, java_rank)

    def test_multiple_verified_uses_combined_history(self):
        extra = self.make_issue(
            self.py_repo, self.editor,
            title='Django authentication middleware',
            description='Fix Django session authentication for REST views.',
            tags=[self.django_tag],
        )
        self.verified_solve(self.user, self.solved_py)
        self.verified_solve(self.user, extra)
        recs = get_recommendations(self.user, limit=6)
        ids = [i.id for i in recs]
        self.assertNotIn(extra.id, ids)
        self.assertNotIn(self.solved_py.id, ids)

    def test_issue_text_uses_available_fields(self):
        text = _issue_text(self.solved_py)
        self.assertIn('django', text)
        self.assertIn('python', text)
        self.assertIn('django-app', text)

    def test_exclusions(self):
        self.verified_solve(self.user, self.solved_py)
        recs = get_recommendations(self.user, limit=20)
        ids = [i.id for i in recs]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn(self.solved_py.id, ids)
        self.assertNotIn(self.closed_py.id, ids)
        self.assertNotIn(self.inactive_issue.id, ids)
        for issue in recs:
            self.assertEqual(issue.status, 'open')
            self.assertTrue(issue.repo.is_active)

    def test_empty_description_missing_tags_no_crash(self):
        recs = get_recommendations(self.user, limit=6)
        self.assertIsInstance(recs, list)

    def test_no_open_issues_returns_empty_or_fallback_safely(self):
        Issue.objects.filter(status='open').update(status='closed')
        recs = get_recommendations(self.user, limit=6)
        self.assertEqual(recs, [])

    def test_only_one_available_issue(self):
        Issue.objects.filter(status='open').exclude(pk=self.open_py_1.pk).update(status='closed')
        recs = get_recommendations(self.user, limit=6)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].id, self.open_py_1.id)


class DashboardRecommendationTests(SolvedMLFixturesMixin, TestCase):
    def setUp(self):
        self.editor = self.make_user('ed3', role='editor')
        self.user = self.make_user('dashuser')
        self.repo = self.make_repo(self.editor, 'dash-repo', language='Python')
        self.open_issue = self.make_issue(self.repo, self.editor, 'Open dashboard issue', description='Python Django views')
        self.closed = self.make_issue(self.repo, self.editor, 'Closed dash issue', status='closed')

    def test_dashboard_has_on_demand_button_not_auto_recs(self):
        self.client.force_login(self.user)
        response = self.client.get('/dashboard/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Find Similar Issues')
        self.assertContains(response, '/issues/recommended/')
        self.assertNotIn('recommended_issues', response.context)

    def test_recommended_page_fallback_with_no_history(self):
        self.client.force_login(self.user)
        response = self.client.get('/issues/recommended/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.open_issue.title)
        self.assertNotContains(response, self.closed.title)

    def test_recommended_page_personalized_when_verified_history_exists(self):
        other = self.make_issue(
            self.repo, self.editor,
            title='Another Django view bug',
            description='Python Django request handling',
        )
        self.verified_solve(self.user, self.open_issue)
        self.client.force_login(self.user)
        response = self.client.get('/issues/recommended/')
        ids = [i.id for i in response.context['recommended_issues']]
        self.assertNotIn(self.open_issue.id, ids)
        self.assertNotIn(self.closed.id, ids)
        self.assertIn(other.id, ids)
        self.assertEqual(response.context['solved_count'], 1)

    def test_recommended_requires_login(self):
        response = self.client.get('/issues/recommended/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response['Location'])


class DashboardSolvedHistoryTests(SolvedMLFixturesMixin, TestCase):
    """The dashboard "Your Solved Issues" section: current user's verified records only."""

    def setUp(self):
        self.editor = self.make_user('ed4', role='editor')
        self.user_a = self.make_user('solvera', github_username='alice')
        self.user_b = self.make_user('solverb', github_username='bob')
        self.repo = self.make_repo(self.editor, 'solved-repo', language='Python')
        self.issue_x = self.make_issue(self.repo, self.editor, 'Issue X bug', description='Python bug X')
        self.issue_y = self.make_issue(self.repo, self.editor, 'Issue Y bug', description='Python bug Y')

    def _dashboard(self, user):
        self.client.force_login(user)
        return self.client.get('/dashboard/')

    def test_dashboard_shows_own_verified_solved_issue(self):
        self.verified_solve(self.user_a, self.issue_x)
        response = self._dashboard(self.user_a)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Your Solved Issues')
        self.assertContains(response, self.issue_x.title)

    def test_other_users_solved_issue_not_shown(self):
        self.verified_solve(self.user_a, self.issue_x)
        response = self._dashboard(self.user_b)
        self.assertNotContains(response, self.issue_x.title)

    def test_unverified_records_not_shown(self):
        SolvedIssue.objects.create(user=self.user_a, issue=self.issue_x, is_verified=False)
        response = self._dashboard(self.user_a)
        self.assertNotContains(response, self.issue_x.title)

    def test_empty_state_when_no_solved_issues(self):
        response = self._dashboard(self.user_a)
        self.assertContains(response, 'No solved issues yet')
        self.assertNotContains(response, self.issue_x.title)

    def test_multiple_solved_issues_displayed(self):
        self.verified_solve(self.user_a, self.issue_x)
        self.verified_solve(self.user_a, self.issue_y)
        response = self._dashboard(self.user_a)
        self.assertContains(response, self.issue_x.title)
        self.assertContains(response, self.issue_y.title)
        self.assertEqual(response.context['solved_count'], 2)

    def test_solved_history_survives_issue_closed_and_shows_closed_badge(self):
        self.verified_solve(self.user_a, self.issue_x)
        self.issue_x.status = 'closed'
        self.issue_x.save()
        response = self._dashboard(self.user_a)
        self.assertContains(response, self.issue_x.title)
        self.assertContains(response, 'Closed')
        # open listings must not show it anymore
        self.client.force_login(self.user_a)
        listing = self.client.get('/issues/')
        self.assertNotContains(listing, self.issue_x.title)

    def test_retention_nulled_issue_does_not_break_dashboard(self):
        self.verified_solve(self.user_a, self.issue_x)
        SolvedIssue.objects.filter(user=self.user_a, issue=self.issue_x).update(issue=None)
        response = self._dashboard(self.user_a)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No solved issues yet')

    def test_dashboard_requires_login(self):
        response = self.client.get('/dashboard/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response['Location'])


class ClosedIssueStatusTests(SolvedMLFixturesMixin, TestCase):
    """Closed issues: detail page reflects CLOSED state and cannot be treated as open."""

    def setUp(self):
        self.editor = self.make_user('ed5', role='editor')
        self.user = self.make_user('closer', github_username='carol')
        self.repo = self.make_repo(self.editor, 'closed-repo', language='Python')
        self.issue = self.make_issue(self.repo, self.editor, 'Soon closed issue', description='Python close me')

    def test_open_issue_detail_offers_tackle_cta(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('issue_detail', args=[self.issue.id]))
        self.assertContains(response, 'Ready to tackle this issue?')

    def test_closed_issue_detail_drops_open_cta_and_shows_closed(self):
        self.issue.status = 'closed'
        self.issue.save()
        self.client.force_login(self.user)
        response = self.client.get(reverse('issue_detail', args=[self.issue.id]))
        self.assertContains(response, 'This issue is closed')
        self.assertNotContains(response, 'Ready to tackle this issue?')

    def test_closing_reflects_on_next_listing_request(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get('/issues/'), self.issue.title)
        self.issue.status = 'closed'
        self.issue.save()
        self.assertNotContains(self.client.get('/issues/'), self.issue.title)
        # reopening restores it per existing implementation
        self.issue.status = 'open'
        self.issue.save()
        self.assertContains(self.client.get('/issues/'), self.issue.title)


class CoreRegressionTests(TestCase):
    def test_landing_page_ok(self):
        self.assertEqual(self.client.get('/').status_code, 200)

    def test_login_page_ok(self):
        self.assertEqual(self.client.get(reverse('login')).status_code, 200)

    def test_issues_list_ok(self):
        self.assertEqual(self.client.get('/issues/').status_code, 200)

    def test_dashboard_requires_login(self):
        response = self.client.get('/dashboard/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response['Location'])
