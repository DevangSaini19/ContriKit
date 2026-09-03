# ContribKit — Open Source Contribution Bridge

> **The bridge between open-source maintainers and first-time contributors.**  
> Built with Django 5/6, Bootstrap 5, WhiteNoise, and PythonAnywhere WSGI serving.

---

## Product Overview

**ContribKit** solves the hardest part of open source: beginner onboarding. Maintainers ("Editors") post structured contribution opportunities enriched with copy-paste repository standard templates, estimated hours, difficulty tags, and direct GitHub links. Beginners ("Viewers") discover curated starter issues without getting lost in massive unfamiliar codebases.

### Key Features
1. **Curated Issue Board (`/issues/`)**: Structured beginner-friendly opportunities filterable by programming language, difficulty level (`Beginner` / `Intermediate`), and tech stack tags (`Python`, `React`, `Django`, `TypeScript`, etc.). Searchable via multi-field keyword queries.
2. **Repository Template Library (`/templates/`)**: Copy-paste-ready standard markdown files (`README.md`, `CONTRIBUTING.md`, `ISSUE_TEMPLATE.md`, `PULL_REQUEST_TEMPLATE.md`, `CODE_OF_CONDUCT.md`) with one-click JS clipboard copying.
3. **Interactive Git Cheat Sheet (`/cheatsheet/`)**: 30+ searchable Git workflows grouped into setup, staging, branching, PR syncing, and undoing mistakes. Instant client-side filtering without page reloads.
4. **GitHub API Integration**: 
   - **URL Auto-fill (`/editor/repos/`)**: Pasting any public GitHub repository URL validates live against `api.github.com`, auto-filling repo name, star counts, primary language, and description.
   - **Bulk Issue Import (`/editor/issues/import/`)**: Bulk-fetches open candidate issues labeled `good first issue`, `beginner`, or `starter` for human review before publishing.
5. **Multi-Tier Role Architecture**:
   - **Viewer**: Default for new signups. Browse issues, explore templates, bookmark issues to dashboard via AJAX, and self-upgrade anytime.
   - **Editor**: Self-upgradable with zero approval bottlenecks. Link repos, post opportunities, import GitHub candidates, and analyze repository view metrics. Can switch back to Viewer anytime without data loss.
   - **Admin**: Full moderation suite. Manage users/roles, moderate/remove issues, toggle featured badges (`⭐ Featured`), CRUD template library files, CRUD cheat sheet sections/commands, and view platform distribution charts.

---

## Platform Screens & Walkthrough

- **Landing Page (`/`)**: High-impact hero CTA, live aggregated database counts (Issues, Templates, Active Repos), and featured opportunities grid.
- **Maintainer Hub (`/dashboard/` & `/editor/repos/`)**: Role switching badges, AJAX bookmark previews, and instant GitHub URL validation.
- **Analytics Dashboards (`/editor/analytics/` & `/admin-panel/analytics/`)**: Visual Chart.js metrics tracking total session views, bookmark save conversions, and role distributions.

---

## Local Development Setup

1. **Clone & Virtual Environment**
   ```bash
   git clone https://github.com/yourusername/contribkit.git
   cd contribkit
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

2. **Install Pinned Dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Environment Variables**
   ```bash
   cp .env.example .env
   # Edit .env if needed (defaults to dev sqlite3 settings)
   ```

4. **Database Migration & Seeding**
   ```bash
   python manage.py migrate
   python manage.py seed_data       # Populates 3+ repos, 10+ issues, 5 templates, 30+ Git commands
   python manage.py collectstatic --noinput
   ```

5. **Run Development Server & Automated Verification**
   ```bash
   python test_all_routes.py        # Runs 100% automated smoke tests across 20+ routes
   python manage.py test            # Unit tests, incl. the full Google OAuth round trip
   python manage.py runserver
   ```
   Visit `http://127.0.0.1:8000/` in your browser.


   Added Deploy Link:- https://shouryano01.pythonanywhere.com/ 

---

## Sign in with Google (OAuth 2.0)

Handled by [python-social-auth](https://python-social-auth.readthedocs.io/) (`social-auth-app-django`), which exposes `/login/google-oauth2/` (start) and `/complete/google-oauth2/` (callback).

### 1. Create the credentials

Google Cloud Console → **APIs & Services → Credentials → Create Credentials → OAuth client ID → Web application**.

### 2. Register the authorized redirect URI

This is the step that most often gets missed. The callback path is **`/complete/google-oauth2/`** on whatever host you browse the site from — including the trailing slash. `localhost` and `127.0.0.1` count as *different* origins, so register each one you actually use:

| Where you run it | Authorized redirect URI |
| --- | --- |
| `runserver`, browsing `localhost` | `http://localhost:8000/complete/google-oauth2/` |
| `runserver`, browsing `127.0.0.1` | `http://127.0.0.1:8000/complete/google-oauth2/` |
| PythonAnywhere | `https://yourusername.pythonanywhere.com/complete/google-oauth2/` |

A missing entry produces `Error 400: redirect_uri_mismatch` from Google.

### 3. Put the credentials in `.env`

```dotenv
GOOGLE_OAUTH2_CLIENT_ID=1234-abcdefghijklmnop.apps.googleusercontent.com
GOOGLE_OAUTH2_CLIENT_SECRET=GOCSPX-...
```

`base.py` reads these into `SOCIAL_AUTH_GOOGLE_OAUTH2_KEY` / `..._SECRET`. Never commit them.

### 4. Migrate

`python manage.py migrate` creates the `social_django` tables (`usersocialauth`, `nonce`, `association`, `partial`).

### Troubleshooting

**The button does nothing, and the server log shows `"POST /login/google-oauth2/ HTTP/1.1" 302 0`.**
Django did its job — it returned a correct 302 to `https://accounts.google.com/o/oauth2/auth?...` — but the *browser* refused to follow it. `core/csp_middleware.py` sends a `Content-Security-Policy` header, and Chrome/Chromium/Safari enforce the `form-action` directive across the **entire redirect chain** of a form submission (Firefox does not). Since the Google button posts a same-origin form whose 302 target is Google, `form-action 'self'` alone blocks the hop and the page just sits there. The fix is to list the IdP host:

```python
# contribkit/settings/base.py
CSP_FORM_ACTION_EXTRA = ['https://accounts.google.com']
```

Add another entry here whenever you wire up an extra OAuth provider. Open DevTools → Console to confirm; a blocked hop is reported as `Refused to send form data to ... because it violates the following Content Security Policy directive: "form-action 'self'"`.

**`Error 400: redirect_uri_mismatch`** — see step 2.

**The callback works but you get bounced back to the login page** — social-auth caught an exception. `SOCIAL_AUTH_RAISE_EXCEPTIONS = False` routes it through `SocialAuthExceptionMiddleware` as a flash message instead of a stack trace, so read the message on the login page rather than the traceback.

**Everything 301-redirects to `https://localhost:8000` and the page won't load** — you are running the *prod* settings module, where `SECURE_SSL_REDIRECT=True`. `manage.py` selects dev settings by default, so this only happens if `DJANGO_SETTINGS_MODULE=contribkit.settings.prod` was exported in your shell. Unset it for local work.
