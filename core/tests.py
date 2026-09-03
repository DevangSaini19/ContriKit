"""Tests for core middleware, including the CSP header that governs the
Google OAuth login redirect.

These run through the real middleware stack and the real
``social_django.views.auth`` / ``social_django.views.complete`` views; only the
two outbound HTTPS calls to Google are mocked.
"""

from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.test import Client, TestCase
from django.urls import reverse


FAKE_CLIENT_ID = '1234567890-abcdefghijklmnop.apps.googleusercontent.com'
FAKE_CLIENT_SECRET = 'GOCSPX-fake-secret'

FAKE_TOKEN_RESPONSE = {
    'access_token': 'ya29.fake-access-token',
    'token_type': 'Bearer',
    'expires_in': 3599,
    'scope': 'openid email profile',
    'id_token': 'fake.jwt.token',
}

FAKE_USERINFO = {
    'sub': '107891505577000000000',
    'email': 'ada@example.com',
    'email_verified': True,
    'name': 'Ada Lovelace',
    'given_name': 'Ada',
    'family_name': 'Lovelace',
    'picture': 'https://lh3.googleusercontent.com/a/fake',
}


def csrf_token(client):
    """Grab a real CSRF token from the login page, as the browser would."""
    response = client.get(reverse('login'))
    assert response.status_code == 200
    return client.cookies['csrftoken'].value


def _parse_csp(header):
    directives = {}
    for part in header.split(';'):
        tokens = part.strip().split()
        if tokens:
            directives[tokens[0]] = tokens[1:]
    return directives


class CSPFormActionTests(TestCase):
    """`form-action` must permit the OAuth provider's consent screen.

    Chrome/Chromium and Safari enforce `form-action` across the entire redirect
    chain of a form submission, so `'self'` on its own silently kills the
    "Sign in with Google" button.
    """

    def test_form_action_allows_google(self):
        response = self.client.get(reverse('login'))
        csp = _parse_csp(response['Content-Security-Policy'])
        self.assertIn("'self'", csp['form-action'])
        self.assertIn('https://accounts.google.com', csp['form-action'])

    def test_form_action_extra_setting_is_respected(self):
        with self.settings(CSP_FORM_ACTION_EXTRA=['https://example.org']):
            response = self.client.get(reverse('login'))
        self.assertEqual(
            _parse_csp(response['Content-Security-Policy'])['form-action'],
            ["'self'", 'https://example.org'],
        )

    def test_missing_form_action_extra_setting_does_not_break(self):
        with self.settings(CSP_FORM_ACTION_EXTRA=None):
            response = self.client.get(reverse('login'))
        self.assertEqual(
            _parse_csp(response['Content-Security-Policy'])['form-action'],
            ["'self'"],
        )


class GoogleOAuthFlowTests(TestCase):
    """Full "Sign in with Google" round trip."""

    def setUp(self):
        self.settings_override = self.settings(
            SOCIAL_AUTH_GOOGLE_OAUTH2_KEY=FAKE_CLIENT_ID,
            SOCIAL_AUTH_GOOGLE_OAUTH2_SECRET=FAKE_CLIENT_SECRET,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.client = Client(enforce_csrf_checks=True)

    def test_login_button_redirects_to_google_consent_screen(self):
        token = csrf_token(self.client)
        response = self.client.post(
            reverse('social:begin', args=['google-oauth2']),
            {'csrfmiddlewaretoken': token},
            HTTP_REFERER='http://testserver/accounts/login/',
        )
        self.assertEqual(response.status_code, 302)
        location = response['Location']
        self.assertTrue(
            location.startswith('https://accounts.google.com/o/oauth2/auth?'),
            f'unexpected redirect target: {location}',
        )
        # Parse the query instead of substring-matching: the assertions then
        # survive harmless reordering/encoding changes by social-auth while
        # still pinning the exact target and parameters.
        params = parse_qs(urlparse(location).query)
        self.assertEqual(params['client_id'], [FAKE_CLIENT_ID])
        self.assertEqual(
            params['redirect_uri'], ['http://testserver/complete/google-oauth2/']
        )

        # The redirect response carries the CSP too; it must still allow the
        # hop to Google, or the browser blocks the navigation.
        self.assertIn(
            'https://accounts.google.com',
            _parse_csp(response['Content-Security-Policy'])['form-action'],
        )
        return location

    def test_full_round_trip_creates_and_logs_in_user(self):
        from accounts.models import User
        from social_django.models import UserSocialAuth

        location = self.test_login_button_redirects_to_google_consent_screen()

        # Pull the `state` Django stashed in the session and echo it back,
        # exactly as Google does on the callback.
        state = parse_qs(urlparse(location).query)['state'][0]

        with mock.patch(
            'social_core.backends.google.GoogleOAuth2.request_access_token',
            return_value=dict(FAKE_TOKEN_RESPONSE),
        ), mock.patch(
            'social_core.backends.google.GoogleOAuth2.user_data',
            return_value=dict(FAKE_USERINFO),
        ):
            response = self.client.get(
                reverse('social:complete', args=['google-oauth2']),
                {'code': '4/0Afake-auth-code', 'state': state, 'scope': 'openid email profile'},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], settings.LOGIN_REDIRECT_URL)

        user = User.objects.get(email='ada@example.com')
        self.assertEqual(user.username, 'ada')
        self.assertEqual(user.first_name, 'Ada')
        self.assertEqual(user.last_name, 'Lovelace')
        self.assertEqual(user.role, 'viewer')

        social = UserSocialAuth.objects.get(user=user)
        self.assertEqual(social.provider, 'google-oauth2')
        self.assertEqual(social.extra_data['access_token'], 'ya29.fake-access-token')

        # The session really is authenticated by the callback itself. Deliberately
        # NO force_login here: forcing a login would overwrite the
        # session and the assertions would pass even if social-auth had failed
        # to authenticate.
        self.assertEqual(self.client.session.get('_auth_user_id'), str(user.pk))
        self.assertEqual(self.client.get('/dashboard/').status_code, 200)

    def test_second_login_reuses_the_same_account(self):
        """A repeat Google login must not create a duplicate user."""
        from accounts.models import User

        for _ in range(2):
            location = self.test_login_button_redirects_to_google_consent_screen()
            state = parse_qs(urlparse(location).query)['state'][0]
            with mock.patch(
                'social_core.backends.google.GoogleOAuth2.request_access_token',
                return_value=dict(FAKE_TOKEN_RESPONSE),
            ), mock.patch(
                'social_core.backends.google.GoogleOAuth2.user_data',
                return_value=dict(FAKE_USERINFO),
            ):
                self.client.get(
                    reverse('social:complete', args=['google-oauth2']),
                    {'code': '4/0Afake-auth-code', 'state': state},
                )
            self.client.logout()

        self.assertEqual(User.objects.filter(email='ada@example.com').count(), 1)
