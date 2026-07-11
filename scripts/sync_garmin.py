import base64
import json
import os
import sys
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
    GarminConnectConnectionError,
)

WINDOW_DAYS = 14
TOKENSTORE = Path(os.environ.get("GARMINTOKENS", "~/.garminconnect")).expanduser()
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "activities.json"


def seed_tokenstore_if_empty():
    """First-ever run (or a cache eviction): if no cached session exists yet,
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


def fail_on_mfa():
    raise RuntimeError(
        "Garmin requires MFA and no valid cached session was found. "
        "Run this script locally once to complete MFA interactively, then "
        "base64-encode ~/.garminconnect/garmin_tokens.json and set it as "
        "the GARMIN_TOKENS_SEED_B64 secret (see README)."
    )


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

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(activities)} activities to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
