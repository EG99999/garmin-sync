"""
Run this ONCE, locally, on your own machine — not in CI.

It opens an interactive device-code login for your personal Microsoft account
(the same one dieta-app already syncs its backup to) and saves the resulting
session to a local file. You then base64-encode that file and paste it as the
MS_GRAPH_TOKEN_CACHE_SEED_B64 secret on the garmin-sync repo. After that,
sync_garmin.py refreshes this session silently on every scheduled run —
you should never need to run this script again unless the session gets
revoked (e.g. you change your Microsoft password).

Usage:
    pip install msal
    python scripts/onedrive_seed_login.py <client-id>

<client-id> is the "Application (client) ID" from the Azure AD app
registration you create for this (see README) — it's a public identifier,
not a secret, safe to pass on the command line or hardcode if you prefer.
"""
import sys
from pathlib import Path

import msal

AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Files.ReadWrite", "User.Read"]
CACHE_FILE = Path("onedrive_token_cache.bin")


def main():
    if len(sys.argv) != 2:
        print("Usage: python onedrive_seed_login.py <azure-app-client-id>")
        sys.exit(1)
    client_id = sys.argv[1]

    cache = msal.SerializableTokenCache()
    app = msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache)

    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Failed to start device flow: {flow}")

    print(flow["message"])  # "To sign in, open https://microsoft.com/devicelogin and enter code XXXXXXX"
    result = app.acquire_token_by_device_flow(flow)  # blocks until you complete login in a browser

    if "access_token" not in result:
        raise RuntimeError(f"Login failed: {result.get('error_description')}")

    CACHE_FILE.write_text(cache.serialize(), encoding="utf-8")
    print(f"\nLogin successful. Session saved to {CACHE_FILE.resolve()}")
    print("Next: base64-encode this file and store it as the MS_GRAPH_TOKEN_CACHE_SEED_B64 secret.")
    print("  macOS/Linux: base64 -i onedrive_token_cache.bin | tr -d '\\n'")
    print("  Windows PowerShell: [Convert]::ToBase64String([IO.File]::ReadAllBytes('onedrive_token_cache.bin'))")


if __name__ == "__main__":
    main()
