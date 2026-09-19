# prcheck.dev — backend

Django REST backend with **GitHub-OAuth-only** authentication and a
production-grade security posture. There is no local password login; accounts
are provisioned exclusively through GitHub.

## Setup

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in the values
python manage.py migrate
python manage.py runserver
```

### Configure the GitHub OAuth App

1. https://github.com/settings/developers → **New OAuth App**
2. **Authorization callback URL** must exactly equal `GITHUB_OAUTH_REDIRECT_URI`
   (default `http://localhost:8000/api/auth/github/callback/`).
3. Put the Client ID / Secret into `.env`:
   ```
   GITHUB_OAUTH_CLIENT_ID=...
   GITHUB_OAUTH_CLIENT_SECRET=...
   ```

## Auth endpoints

| Method | Path                              | Auth        | Purpose                                             |
|--------|-----------------------------------|-------------|-----------------------------------------------------|
| GET    | `/api/auth/github/login/`         | none        | 302 → GitHub; sets a signed, single-use state cookie |
| GET    | `/api/auth/github/callback/`      | none        | Validates state, exchanges code, issues tokens       |
| POST   | `/api/auth/token/refresh/`        | refresh cookie | Rotates refresh cookie, returns a new access token |
| POST   | `/api/auth/logout/`               | access token | Blacklists the refresh token, clears the cookie     |
| GET    | `/api/auth/me/`                   | access token | Current user profile                                 |
| GET    | `/api/health/`                    | none        | Liveness probe                                        |

### Token model

- **Access token** — 15 min, `HS256` JWT, returned in the JSON body. Clients send
  it back as `Authorization: Bearer <token>` (header-based ⇒ no CSRF surface).
- **Refresh token** — 14 days, delivered **only** as a cookie that is
  `httpOnly` + `Secure` + `SameSite=Strict`, scoped to `Path=/api/auth`. Invisible
  to JavaScript, so XSS cannot exfiltrate it.
- **Rotation + blacklist** — every refresh mints a new refresh token and
  blacklists the old one (`token_blacklist` app). A stolen/replayed refresh token
  is rejected after first use.

### The OAuth flow, step by step

1. Browser hits `/api/auth/github/login/`. The server generates a random `state`,
   stores it in a signed httpOnly `SameSite=Lax` cookie, and 302s to GitHub.
2. User authorizes on GitHub; GitHub redirects back to the callback with
   `code` + `state`.
3. The server compares `state` against the cookie **in constant time** (CSRF
   defense), exchanges `code` for a GitHub access token, reads the profile and a
   **verified** primary email, and upserts the local user (matched on the
   immutable GitHub numeric id).
4. The server returns an access token + user JSON and sets the refresh cookie.
   The state cookie is deleted (single-use).

## Security posture

- **Env-driven config** (`django-environ`): secrets never live in code. The
  process refuses to boot in production without `DJANGO_SECRET_KEY`.
- **HTTPS enforcement when `DJANGO_DEBUG=False`**: `SECURE_SSL_REDIRECT`, HSTS
  (1 yr, subdomains, preload), `SECURE_PROXY_SSL_HEADER` for TLS-terminating
  proxies, secure session/CSRF cookies.
- **Headers**: `X-Frame-Options: DENY`, `nosniff`, referrer policy.
- **Argon2** password hashing (for the rare superuser).
- **Throttling** (DRF): `20/hour` on auth endpoints (brute-force guard),
  `60/hour` anon, `1000/hour` per user.
- **JSON-only renderer** in production (browsable API only in DEBUG).
- `python manage.py check --deploy` passes with zero warnings.

## Tests

```bash
python manage.py test accounts
```

Covers the full lifecycle (login → me → refresh → logout), state/CSRF rejection,
refresh-token rotation & replay blacklisting, and OAuth-only (no usable password)
account provisioning. GitHub calls are mocked, so tests run offline.

## Production notes

- Set `DJANGO_DEBUG=False`, a strong `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`,
  and `DJANGO_CSRF_TRUSTED_ORIGINS`.
- Use Postgres via `DATABASE_URL` (add `psycopg[binary]` to requirements).
- Serve behind TLS; run `collectstatic`; serve via gunicorn/uvicorn + a proxy.
