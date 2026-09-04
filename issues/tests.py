from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from issues.models import Issue, SolvedIssue, Tag
from issues.recommender import SKLEARN_AVAILABLE, _issue_text, get_recommendations
from repos.models import Repo

User = get_user_model()


class SolvedMLFixturesMixin:
    def make_user(self, username, role='viewer'):
        return User.objects.create_user(
            username=username,
            email=f'{username}@example.com',
            password='pass12345',
            role=role,
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


class MarkSolvedViewTests(SolvedMLFixturesMixin, TestCase):
    def setUp(self):
        self.editor = self.make_user('editor1', role='editor')
        self.user_a = self.make_user('usera')
        self.user_b = self.make_user('userb')
        self.repo = self.make_repo(self.editor, 'kit')
        self.issue = self.make_issue(self.repo, self.editor, 'Fix login bug')
        self.closed = self.make_issue(self.repo, self.editor, 'Closed bug', status='closed')
        self.url = reverse('mark_solved', args=[self.issue.id])
        self.closed_url = reverse('mark_solved', args=[self.closed.id])

    def authenticated_csrf_client(self, user):
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        response = client.get('/issues/')
        self.assertEqual(response.status_code, 200)
        return client, client.cookies['csrftoken'].value

    def test_solved_button_on_open_issue_for_authenticated_contributor(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse('issue_detail', args=[self.issue.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'btn-mark-solved')
        self.assertContains(response, f'data-issue-id="{self.issue.id}"')

    def test_post_creates_solvedissue_with_solved_at(self):
        client, token = self.authenticated_csrf_client(self.user_a)
        response = client.post(self.url, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'solved')
        self.assertEqual(data['issue_id'], self.issue.id)
        self.assertIn('solved_at', data)
        obj = SolvedIssue.objects.get(user=self.user_a, issue=self.issue)
        self.assertIsNotNone(obj.solved_at)

    def test_csrf_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user_a)
        response = client.post(self.url)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SolvedIssue.objects.filter(user=self.user_a, issue=self.issue).exists())

    def test_unauthenticated_rejected(self):
        client = Client(enforce_csrf_checks=True)
        login_page = client.get(reverse('login'))
        self.assertEqual(login_page.status_code, 200)
        token = client.cookies['csrftoken'].value
        response = client.post(self.url, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error'], 'login_required')

    def test_duplicate_solving_same_user_rejected(self):
        client, token = self.authenticated_csrf_client(self.user_a)
        first = client.post(self.url, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(first.status_code, 200)
        second = client.post(self.url, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.json()['error'], 'already_solved')
        self.assertEqual(SolvedIssue.objects.filter(user=self.user_a, issue=self.issue).count(), 1)

    def test_multiple_users_can_solve_same_issue(self):
        client_a, token_a = self.authenticated_csrf_client(self.user_a)
        client_b, token_b = self.authenticated_csrf_client(self.user_b)
        self.assertEqual(client_a.post(self.url, HTTP_X_CSRFTOKEN=token_a).status_code, 200)
        self.assertEqual(client_b.post(self.url, HTTP_X_CSRFTOKEN=token_b).status_code, 200)
        self.assertEqual(SolvedIssue.objects.filter(issue=self.issue).count(), 2)

    def test_db_unique_constraint_prevents_duplicate(self):
        SolvedIssue.objects.create(user=self.user_a, issue=self.issue)
        with self.assertRaises(IntegrityError):
            SolvedIssue.objects.create(user=self.user_a, issue=self.issue)

    def test_closed_issue_hides_solved_button(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse('issue_detail', args=[self.closed.id]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'btn-mark-solved')
        self.assertContains(response, 'This issue is closed')

    def test_direct_post_to_closed_issue_rejected(self):
        client, token = self.authenticated_csrf_client(self.user_a)
        response = client.post(self.closed_url, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'issue_closed')

    def test_closed_issues_excluded_from_listings(self):
        self.client.force_login(self.user_a)
        response = self.client.get('/issues/')
        self.assertContains(response, self.issue.title)
        self.assertNotContains(response, self.closed.title)

    def test_solved_history_preserved_when_issue_closed(self):
        SolvedIssue.objects.create(user=self.user_a, issue=self.issue)
        self.issue.status = 'closed'
        self.issue.save()
        self.assertTrue(SolvedIssue.objects.filter(user=self.user_a, issue=self.issue).exists())
        self.assertIsNotNone(Issue.objects.get(pk=self.issue.pk).closed_at)

    def test_reopen_clears_closed_at(self):
        self.issue.status = 'closed'
        self.issue.save()
        self.assertIsNotNone(self.issue.closed_at)
        self.issue.status = 'open'
        self.issue.save()
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, 'open')
        self.assertIsNone(self.issue.closed_at)

    def test_detail_ui_updates_after_solved(self):
        client, token = self.authenticated_csrf_client(self.user_a)
        client.post(self.url, HTTP_X_CSRFTOKEN=token)
        response = client.get(reverse('issue_detail', args=[self.issue.id]))
        self.assertContains(response, 'You marked this as solved')
        self.assertNotContains(response, 'btn-mark-solved')


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
        solved = SolvedIssue.objects.create(user=self.user, issue=self.expired)
        call_command('cleanup_closed_issues', '--days', '30', stdout=StringIO())
        solved.refresh_from_db()
        self.assertIsNone(solved.issue)
        self.assertEqual(solved.user_id, self.user.id)

    def test_two_deleted_issues_preserve_both_solved_rows(self):
        other = self.make_issue(self.repo, self.editor, 'Another expired', status='closed')
        Issue.objects.filter(pk=other.pk).update(closed_at=timezone.now() - timedelta(days=40))
        s1 = SolvedIssue.objects.create(user=self.user, issue=self.expired)
        s2 = SolvedIssue.objects.create(user=self.user, issue=other)
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

    def test_zero_solved_fallback_no_error(self):
        recs = get_recommendations(self.user, limit=6)
        self.assertGreater(len(recs), 0)
        ids = [i.id for i in recs]
        self.assertNotIn(self.closed_py.id, ids)
        self.assertNotIn(self.inactive_issue.id, ids)

    def test_one_solved_influences_recommendations(self):
        SolvedIssue.objects.create(user=self.user, issue=self.solved_py)
        recs = get_recommendations(self.user, limit=6)
        ids = [i.id for i in recs]
        self.assertNotIn(self.solved_py.id, ids)
        self.assertIn(self.open_py_1.id, ids)
        py_rank = ids.index(self.open_py_1.id)
        java_rank = ids.index(self.open_java.id) if self.open_java.id in ids else 99
        self.assertLess(py_rank, java_rank)

    def test_multiple_solved_uses_combined_history(self):
        extra = self.make_issue(
            self.py_repo, self.editor,
            title='Django authentication middleware',
            description='Fix Django session authentication for REST views.',
            tags=[self.django_tag],
        )
        SolvedIssue.objects.create(user=self.user, issue=self.solved_py)
        SolvedIssue.objects.create(user=self.user, issue=extra)
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
        SolvedIssue.objects.create(user=self.user, issue=self.solved_py)
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

    def test_fallback_copy_with_no_history(self):
        self.client.force_login(self.user)
        response = self.client.get('/dashboard/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Recommended for You')
        self.assertContains(response, 'Popular picks')
        self.assertContains(response, self.open_issue.title)
        self.assertNotContains(response, self.closed.title)

    def test_personalized_when_history_exists(self):
        other = self.make_issue(
            self.repo, self.editor,
            title='Another Django view bug',
            description='Python Django request handling',
        )
        SolvedIssue.objects.create(user=self.user, issue=self.open_issue)
        self.client.force_login(self.user)
        response = self.client.get('/dashboard/')
        self.assertContains(response, 'Personalized')
        ids = [i.id for i in response.context['recommended_issues']]
        self.assertNotIn(self.open_issue.id, ids)
        self.assertNotIn(self.closed.id, ids)
        self.assertIn(other.id, ids)
        self.assertEqual(response.context['solved_count'], 1)


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
