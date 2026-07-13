import base64
import json
import os
import sys
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

import requests
import msal
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
    GarminConnectConnectionError,
)

WINDOW_DAYS = 14          # activities: cheap range call, full window every run
RECENT_DAYS = 2           # health metrics: expensive per-day calls, only today+yesterday every run
TOKENSTORE = Path(os.environ.get("GARMINTOKENS", "~/.garminconnect")).expanduser()
ACTIVITIES_PATH = Path(__file__).resolve().parent.parent / "docs" / "activities.json"

# Microsoft Graph / OneDrive — private destination for sensitive health metrics
# (activities.json above stays public on Pages; this does not)
MS_CLIENT_ID = os.environ.get("MS_GRAPH_CLIENT_ID", "")
MS_AUTHORITY = "https://login.microsoftonline.com/consumers"
MS_SCOPES = ["Files.ReadWrite", "User.Read"]
MS_TOKEN_CACHE_PATH = Path(os.environ.get("MS_GRAPH_TOKEN_CACHE_PATH", "~/.ms_graph_token/cache.bin")).expanduser()
HEALTH_ONEDRIVE_PATH = "/me/drive/root:/GarminHealth-feed.json"

# Per-day-only endpoints (no date-range support) — one API call PER DAY PER METRIC,
# so we only ever ask for RECENT_DAYS days per run and merge into what's already
# published, instead of re-pulling a full window every time.
PER_DAY_METHODS = {
    "sleep": "get_sleep_data",
    "hrv": "get_hrv_data",
    "restingHeartRate": "get_heart_rates",
    "stress": "get_all_day_stress",
    "steps": "get_steps_data",
    "vo2max": "get_max_metrics",
    "trainingStatus": "get_training_status",
    "trainingReadiness": "get_training_readiness",
    "spo2": "get_spo2_data",
    "respiration": "get_respiration_data",
    "floors": "get_floors",
    "hydration": "get_hydration_data",
}


def seed_tokenstore_if_empty():
    """First-ever run (or a cache eviction): if no cached Garmin session exists yet,
    seed it from a base64-encoded token JSON stored as a GitHub secret. This
    lets scheduled runs skip the MFA-requiring interactive login. Only needed
    if the Garmin account has MFA enabled — see README."""
    if TOKENSTORE.exists() and any(TOKENSTORE.iterdir()):
        return
    seed_b64 = os.environ.get("GARMIN_TOKENS_SEED_B64")
    if not seed_b64:
        return
    TOKENSTORE.mkdir(parents=True, exist_ok=True)
    raw = base64.b64decode(seed_b64)
    (TOKENSTORE / "garmin_tokens.json").write_bytes(raw)


def seed_ms_cache_if_empty():
    """Same idea as seed_tokenstore_if_empty(), for the Microsoft Graph session:
    first run (or a cache eviction) restores from the one-time device-flow login
    you did locally via onedrive_seed_login.py. After that, acquire_token_silent
    refreshes it on its own — this only fires when there's nothing to refresh yet."""
    if MS_TOKEN_CACHE_PATH.exists():
        return
    seed_b64 = os.environ.get("MS_GRAPH_TOKEN_CACHE_SEED_B64")
    if not seed_b64:
        return
    MS_TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MS_TOKEN_CACHE_PATH.write_bytes(base64.b64decode(seed_b64))


def fail_on_mfa():
    raise RuntimeError(
        "Garmin requires MFA and no valid cached session was found. "
        "Run this script locally once to complete MFA interactively, then "
        "base64-encode ~/.garminconnect/garmin_tokens.json and set it as "
        "the GARMIN_TOKENS_SEED_B64 secret (see README)."
    )


def sync_activities(client):
    start_date = (date.today() - timedelta(days=WINDOW_DAYS)).isoformat()
    end_date = date.today().isoformat()

    raw_activities = client.get_activities_by_date(start_date, end_date, sortorder="desc")

    activities = []
    for a in raw_activities:
        activity_type = a.get("activityType") or {}
        event_type = a.get("eventType")  # unverified field name, pass through if present
        start_local = a.get("startTimeLocal")  # "YYYY-MM-DD HH:MM:SS"
        if not start_local:
            continue
        iso_local = start_local.replace(" ", "T")

        activities.append({
            "garminId": str(a.get("activityId")),
            "date": iso_local[:10],
            "startTimeLocal": iso_local,
            "type": activity_type.get("typeKey", "unknown"),
            "eventType": (event_type or {}).get("typeKey") if isinstance(event_type, dict) else None,
            "name": a.get("activityName"),
            "durationMin": round((a.get("duration") or 0) / 60, 1),
            "calories": round(a.get("calories") or 0),
        })

    payload = {
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "windowDays": WINDOW_DAYS,
        "activities": activities,
    }

    ACTIVITIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    ACTIVITIES_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(activities)} activities to {ACTIVITIES_PATH}")


def fetch_per_day_metrics(client, d):
    """One dict per calendar day: {metricName: raw_api_response_or_None}.
    Each metric is independently try/excepted so a single flaky/missing
    endpoint (e.g. no SpO2 sensor on the device) doesn't drop the others."""
    iso = d.isoformat()
    out = {}
    for key, method_name in PER_DAY_METHODS.items():
        try:
            method = getattr(client, method_name)
            out[key] = method(iso)
        except Exception as e:
            print(f"::warning::{key} ({method_name}) failed for {iso}: {e}", file=sys.stderr)
            out[key] = None
    return out


def fetch_range_metrics(client, start_d, end_d):
    """The handful of endpoints that DO accept a date range — one call covers
    the whole window, so these can safely span more days than RECENT_DAYS."""
    start_iso, end_iso = start_d.isoformat(), end_d.isoformat()
    out = {}
    for key, call in {
        "bodyBattery": lambda: client.get_body_battery(start_iso, end_iso),
        "bodyComposition": lambda: client.get_body_composition(start_iso, end_iso),
        "dailySteps": lambda: client.get_daily_steps(start_iso, end_iso),
    }.items():
        try:
            out[key] = call()
        except Exception as e:
            print(f"::warning::{key} range fetch failed: {e}", file=sys.stderr)
            out[key] = None
    return out


def merge_range_metric_into_days(days, metric_key, raw_list):
    """Range endpoints return a list of per-day entries. The exact date-field
    name isn't confirmed from source, so we try the common candidates and
    skip an entry (rather than guess wrong) if none match."""
    if not isinstance(raw_list, list):
        return
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        day_str = entry.get("calendarDate") or entry.get("date")
        if not day_str:
            continue
        days.setdefault(day_str, {})[metric_key] = entry


# ── Microsoft Graph (private OneDrive destination) ──

def graph_acquire_token():
    if not MS_CLIENT_ID:
        print("::warning::MS_GRAPH_CLIENT_ID not set, skipping health-metrics OneDrive upload", file=sys.stderr)
        return None

    seed_ms_cache_if_empty()
    cache = msal.SerializableTokenCache()
    if MS_TOKEN_CACHE_PATH.exists():
        cache.deserialize(MS_TOKEN_CACHE_PATH.read_text(encoding="utf-8"))

    app = msal.PublicClientApplication(MS_CLIENT_ID, authority=MS_AUTHORITY, token_cache=cache)
    accounts = app.get_accounts()
    if not accounts:
        print(
            "::warning::No cached Microsoft account — seed the token cache via "
            "onedrive_seed_login.py (see README), skipping this run's OneDrive upload",
            file=sys.stderr,
        )
        return None

    result = app.acquire_token_silent(MS_SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        print(f"::warning::Silent Microsoft token acquisition failed: {result}", file=sys.stderr)
        return None

    if cache.has_state_changed:
        MS_TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        MS_TOKEN_CACHE_PATH.write_text(cache.serialize(), encoding="utf-8")

    return result["access_token"]


def graph_get_json(token, path):
    r = requests.get(
        f"https://graph.microsoft.com/v1.0{path}:/content",
        headers={"Authorization": f"Bearer {token}"},
    )
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    return r.json()


def graph_put_json(token, path, payload):
    r = requests.put(
        f"https://graph.microsoft.com/v1.0{path}:/content",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    r.raise_for_status()


def sync_health(client):
    token = graph_acquire_token()
    if not token:
        return  # health-metrics upload is best-effort/optional; activities sync already succeeded

    try:
        existing_days = graph_get_json(token, HEALTH_ONEDRIVE_PATH).get("days", {})
    except Exception as e:
        print(f"::warning::Could not read existing GarminHealth-feed.json, starting fresh: {e}", file=sys.stderr)
        existing_days = {}

    today_d = date.today()

    # Expensive per-day metrics: only the last RECENT_DAYS days, merged on top
    # of whatever history is already in OneDrive (yesterday may have been
    # incomplete when it was fetched as "today" in a previous run — refetching
    # it once more corrects that, then it ages out of the recent window).
    for i in range(RECENT_DAYS):
        d = today_d - timedelta(days=i)
        existing_days.setdefault(d.isoformat(), {}).update(fetch_per_day_metrics(client, d))

    # Cheap range metrics: fine to cover a slightly wider window each run.
    range_data = fetch_range_metrics(client, today_d - timedelta(days=RECENT_DAYS), today_d)
    for metric_key in ("bodyBattery", "dailySteps"):
        merge_range_metric_into_days(existing_days, metric_key, range_data.get(metric_key))
    # Body composition/weight is sparse (only days you actually weighed in) —
    # same merge helper handles that fine, empty days just get no weight key.
    merge_range_metric_into_days(existing_days, "bodyComposition", range_data.get("bodyComposition"))

    payload = {
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days": existing_days,
    }
    graph_put_json(token, HEALTH_ONEDRIVE_PATH, payload)
    print(f"Uploaded health data for {len(existing_days)} accumulated days to OneDrive ({HEALTH_ONEDRIVE_PATH})")


def main():
    seed_tokenstore_if_empty()

    client = Garmin(
        email=os.environ.get("GARMIN_EMAIL"),
        password=os.environ.get("GARMIN_PASSWORD"),
        prompt_mfa=fail_on_mfa,
    )

    try:
        client.login(str(TOKENSTORE))
    except GarminConnectAuthenticationError as e:
        print(f"::error::Garmin auth failed: {e}", file=sys.stderr)
        sys.exit(1)
    except GarminConnectTooManyRequestsError as e:
        print(f"::warning::Garmin rate-limited this run, skipping: {e}", file=sys.stderr)
        sys.exit(0)  # transient, don't fail the whole workflow
    except GarminConnectConnectionError as e:
        print(f"::error::Garmin connection error: {e}", file=sys.stderr)
        sys.exit(1)

    sync_activities(client)
    sync_health(client)


if __name__ == "__main__":
    main()
