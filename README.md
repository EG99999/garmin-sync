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

## Known fragility

This is a reverse-engineered API, not an official Garmin integration. It can break if
Garmin changes their mobile auth flow (it happened once already, which is why the older
`garth` library is deprecated). A broken run just shows a red X on the Actions tab —
nothing else depends on this being perfectly reliable.
