"""
authorize_gmail.py — One-time OAuth consent for Gmail API.

Run this ONCE from the project root:
    python authorize_gmail.py

What it does:
  1. Reads credentials-gmail-pricing.json  (OAuth 2.0 client — downloaded from Google Cloud Console)
  2. Opens your default browser → log in as ai@six10ventures.com → click "Allow"
  3. Saves gmail_token.json in the project root

After this, the pricing engine (main.py) can send email automatically
with NO browser, NO password, and NO re-consent — until you explicitly revoke access.
The token auto-refreshes silently when it expires.

Re-run this script only if:
  - gmail_token.json is deleted or corrupted
  - You see "Failed to refresh Gmail token" in pricing_engine.log
  - You change the Google account or revoke access in myaccount.google.com
"""

import json
import os
import sys
from pathlib import Path

# ── Resolve paths from config.yaml if present, else use defaults ──────────────
CREDENTIALS_PATH = "credentials-gmail-pricing.json"
TOKEN_PATH       = "gmail_token.json"
SCOPES           = ["https://www.googleapis.com/auth/gmail.send"]

try:
    import yaml
    if Path("config.yaml").exists():
        with open("config.yaml") as f:
            cfg = yaml.safe_load(f)
        CREDENTIALS_PATH = cfg.get("email", {}).get("gmail_credentials_path", CREDENTIALS_PATH)
        TOKEN_PATH       = cfg.get("email", {}).get("gmail_token_path", TOKEN_PATH)
except Exception:
    pass  # Fall back to defaults above


def main():
    # ── Check dependencies ────────────────────────────────────────────────────
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        print("ERROR: google-auth-oauthlib is not installed.")
        print("Run:  pip install -r requirements.txt")
        sys.exit(1)

    # ── Check credentials file exists ─────────────────────────────────────────
    if not Path(CREDENTIALS_PATH).exists():
        print(f"ERROR: OAuth credentials file not found: {CREDENTIALS_PATH}")
        print()
        print("To get it:")
        print("  1. Go to https://console.cloud.google.com/")
        print("  2. APIs & Services -> Credentials")
        print("  3. Find the OAuth 2.0 Client ID for this project")
        print("  4. Download JSON -> save as credentials-gmail-pricing.json in project root")
        sys.exit(1)

    # ── If token already exists and is valid, nothing to do ───────────────────
    token_file = Path(TOKEN_PATH)
    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
            if creds.valid:
                print(f"gmail_token.json already exists and is valid at: {TOKEN_PATH}")
                print("No action needed. The engine can send email as-is.")
                print()
                print("To re-authorise (e.g. after revoking access), delete gmail_token.json and re-run.")
                return
            elif creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(token_file, "w") as f:
                    f.write(creds.to_json())
                print(f"Token refreshed and saved to: {TOKEN_PATH}")
                return
        except Exception as exc:
            print(f"Existing token invalid ({exc}) — re-authorising...")

    # ── Run OAuth consent flow ────────────────────────────────────────────────
    print("=" * 60)
    print("  Six10 Pricing Engine — Gmail OAuth Setup")
    print("=" * 60)
    print()
    print(f"  Credentials file : {CREDENTIALS_PATH}")
    print(f"  Token will save  : {TOKEN_PATH}")
    print(f"  Gmail scope      : gmail.send (send email only, no read access)")
    print()
    print("  Your browser will open. Log in as: ai@six10ventures.com")
    print("  Click 'Allow' to grant send-email permission.")
    print()
    input("  Press ENTER to open the browser...")

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)

    # run_local_server starts a temporary localhost callback server
    # port=0 means pick any available port automatically
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    # Save the token
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())

    print()
    print(f"  Token saved to: {TOKEN_PATH}")
    print()
    print("  Done! The engine can now send email without any further sign-in.")
    print("  Run:  python main.py  (or python main.py --no-email to skip)")


if __name__ == "__main__":
    main()
