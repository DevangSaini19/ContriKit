"""Content Security Policy middleware for ContribKit.

Adds security headers to every response to prevent XSS, data injection,
and other client-side attacks. This is intentionally permissive enough
to allow Bootstrap CDN, Chart.js, and highlight.js which the app uses.
"""

import re

from django.conf import settings


class CSPMiddleware:
    """Adds Content-Security-Policy and Permissions-Policy headers."""

    def __init__(self, get_response):
        self.get_response = get_response

    def _form_action(self):
        """Build the `form-action` source list.

        `'self'` alone is NOT enough for "Sign in with Google". The login page
        POSTs a same-origin form to /login/google-oauth2/, and Django answers
        with a 302 to https://accounts.google.com/o/oauth2/auth. Chrome,
        Chromium and Safari enforce `form-action` against the *entire* redirect
        chain of a form submission (Firefox does not), so the identity
        provider's host must be listed too — otherwise the browser silently
        refuses the navigation, the page never leaves the login screen, and
        Django just logs a healthy `"POST /login/google-oauth2/" 302 0`.

        Extra hosts come from the CSP_FORM_ACTION_EXTRA setting so that
        additional OAuth providers can be allowed without touching this file.
        """
        extra = getattr(settings, 'CSP_FORM_ACTION_EXTRA', None) or []
        sources = ["'self'"] + [s for s in extra if s]
        return 'form-action ' + ' '.join(sources)

    def __call__(self, request):
        response = self.get_response(request)

        # Content Security Policy
        response['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
            "https://cdn.jsdelivr.net "
            "https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' "
            "https://cdn.jsdelivr.net "
            "https://cdnjs.cloudflare.com; "
            "img-src 'self' data: https:; "
            "font-src 'self' https://cdn.jsdelivr.net; "
            "connect-src 'self' https://api.github.com; "
            "frame-ancestors 'none'; "
            f"{self._form_action()}; "
            "base-uri 'self'; "
            "object-src 'none'"
        )

        # Permissions Policy
        response['Permissions-Policy'] = (
            "camera=(), "
            "microphone=(), "
            "geolocation=(), "
            "payment=(), "
            "usb=(), "
            "magnetometer=(), "
            "accelerometer=(), "
            "gyroscope=()"
        )

        return response
