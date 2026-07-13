# garmin-sync

Pulls your recent Garmin Connect activities (type, time, calories) on a schedule and
publishes them as a small public JSON feed via GitHub Pages, so any of your apps can
`fetch()` your workout data without touching Garmin credentials themselves.

Built because Garmin's official Connect Developer Program rejects personal-use
applications (and is currently closed to new applicants entirely). This uses the
unofficial, actively-maintained [`garminconnect`](https://github.com/cyberjunky/python-garminconnect)
Python client instead, which logs in the same way the Garmin mobile app does.

## Feed

`docs/activities.json`, served at `https://<username>.github.io/garmin-sync/activities.json`:

```json
{
  "updatedAt": "2026-07-11T18:00:00Z",
  "windowDays": 14,
  "activities": [
    {
      "garminId": "123456789",
      "date": "2026-07-11",
      "startTimeLocal": "2026-07-11T07:15:00",
      "type": "cycling",
      "eventType": null,
      "name": "Morning Ride",
      "durationMin": 62,
      "calories": 480
    }
  ]
}
```

`type` is Garmin's own raw activity type key (`cycling`, `running`, `lap_swimming`,
`strength_training`, `multi_sport`, …) — deliberately left unmapped here so any consumer
can apply its own mapping. Nothing sensitive is published: no location, no account info,
no tokens.

### Health metrics (private, not on this feed)

Sleep, HRV, resting heart rate, stress, VO2max/training status, body composition, SpO2,
respiration, floors, hydration, and step/Body Battery detail are collected too, but — unlike
activities — these are meaningfully sensitive, so they are **not** published here. They go
straight to a private file on your own OneDrive (`GarminHealth-feed.json`), uploaded by the
workflow, never committed to this repo. See "Private health metrics setup" below.

## Setup

1. **Repo secrets** (Settings → Secrets and variables → Actions), set by you — this repo's
   workflow never has these typed in by anyone but you:
   - `GARMIN_EMAIL` — your Garmin Connect login email
   - `GARMIN_PASSWORD` — your Garmin Connect login password
   - `GARMIN_TOKENS_SEED_B64` — **only if your Garmin account has MFA/2FA enabled**, see below

2. **Enable Pages**: Settings → Pages → Source → "Deploy from a branch" → branch `main`,
   folder `/docs`.

3. **First run**: Actions tab → "Sync Garmin activities" → Run workflow. Check the logs.

### If your Garmin account has MFA enabled

The scheduled job can't answer a live 2FA prompt. You need to seed a valid session once,
from your own machine, so the job can silently refresh it afterward:

```bash
pip install garminconnect curl_cffi
python3 -c "
from garminconnect import Garmin
g = Garmin(email='you@example.com', password='yourpassword')
g.login('~/.garminconnect')   # will prompt for your MFA code interactively
"
```

Then base64-encode the resulting token file and store it as `GARMIN_TOKENS_SEED_B64`:

```bash
# macOS/Linux
base64 -i ~/.garminconnect/garmin_tokens.json | tr -d '\n'
# Windows PowerShell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("$HOME\.garminconnect\garmin_tokens.json"))
```

Paste the output as the `GARMIN_TOKENS_SEED_B64` secret value. The workflow only uses this
seed on the very first run (or after a cache eviction) — after that it keeps itself logged
in via the refresh token, no re-seeding needed unless the session gets revoked (e.g. you
change your Garmin password).

If you don't have MFA enabled, skip this — plain `GARMIN_EMAIL`/`GARMIN_PASSWORD` login
works on every scheduled run.

## Private health metrics setup (optional)

Skip this section entirely if you only want the public activities feed — everything above
already works standalone. This section adds the private OneDrive upload of sleep/HRV/stress/
etc. It's optional and fails silently (a log warning, nothing breaks) if not configured.

1. **Register a new, separate Azure AD app** — deliberately not reusing any existing app
   registration, so nothing here can affect other apps' sign-in. Go to
   [portal.azure.com](https://portal.azure.com) → App registrations → New registration:
   - Name: anything, e.g. `garmin-sync-onedrive`
   - Supported account types: **Personal Microsoft accounts only**
   - Redirect URI: leave blank
   - After creation: **Authentication** (left sidebar) → **Add a platform** → **Mobile and
     desktop applications** → check the default redirect URI checkbox → Configure. Then under
     **Advanced settings** on the same page, set **"Allow public client flows"** to **Yes** →
     Save. This is required for the device-code login below.
   - Copy the **Application (client) ID** from the app's Overview page — this is a public
     identifier, not a secret.

2. **One-time interactive login**, from your own machine (not CI):
   ```bash
   pip install msal
   python scripts/onedrive_seed_login.py <application-client-id>
   ```
   It prints a URL and a short code. Open the URL in any browser, sign in with the Microsoft
   account you want (the one dieta-app already uses), enter the code. On success it writes
   `onedrive_token_cache.bin` in your current directory and prints base64-encoding commands
   for it.

3. **Repo config** (Settings → Secrets and variables → Actions):
   - Under **Variables** tab: `MS_GRAPH_CLIENT_ID` = the Application (client) ID from step 1
     (not sensitive, safe as a plain variable)
   - Under **Secrets** tab: `MS_GRAPH_TOKEN_CACHE_SEED_B64` = the base64 output from step 2

That's it — the workflow picks these up automatically next run. Like the Garmin session, this
refreshes itself silently afterward; you shouldn't need to redo the interactive login unless
the session gets revoked (password change, or the job not running successfully for a stretch —
Microsoft expires an unused public-client refresh token after ~24h of inactivity, well outside
this job's normal 3h schedule).

## Manual trigger from another app (optional)

Consumers (like dieta-app) can offer a "sync now" button that calls GitHub's API directly:

```
POST https://api.github.com/repos/<owner>/garmin-sync/actions/workflows/sync.yml/dispatches
Authorization: Bearer <token>
Content-Type: application/json

{"ref": "main"}
```

This needs a token, and since the calling app may be a public static site, the token must be
scoped as narrowly as possible:

1. github.com → Settings → Developer settings → **Personal access tokens → Fine-grained tokens** → Generate new token
2. **Repository access**: "Only select repositories" → `garmin-sync` (this repo only, nothing else)
3. **Permissions**: under "Repository permissions", set **Actions** to **Read and write**. Leave every other permission at "No access" — in particular, do *not* grant Contents write, so this token can trigger runs but can't push code or read/change secrets.
4. Generate, copy the token, paste it into the consuming app's own config yourself (never share it back through an assistant/agent) wherever that app expects it.

Worst case if this token leaks: someone can spam-trigger the sync workflow. Annoying (risks
Garmin login rate-limiting) but not a data or account compromise, given the restricted scope
above.

## Known fragility

This is a reverse-engineered API, not an official Garmin integration. It can break if
Garmin changes their mobile auth flow (it happened once already, which is why the older
`garth` library is deprecated). A broken run just shows a red X on the Actions tab —
nothing else depends on this being perfectly reliable.
