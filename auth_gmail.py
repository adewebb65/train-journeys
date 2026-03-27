#!/usr/bin/env python3
"""
Run this once to authorise Gmail access. Paste the URL into your browser,
approve access, then paste the authorisation code back here.
"""

import os
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(SCRIPT_DIR, "credentials.json")
TOKEN_FILE = os.path.join(SCRIPT_DIR, "token.json")
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

flow = InstalledAppFlow.from_client_secrets_file(
    CREDENTIALS_FILE,
    SCOPES,
    redirect_uri="urn:ietf:wg:oauth:2.0:oob",
)

auth_url, _ = flow.authorization_url(prompt="consent")
print("\nOpen this URL in your browser:\n")
print(auth_url)
print()

code = input("Paste the authorisation code here: ").strip()
flow.fetch_token(code=code)

creds = flow.credentials
with open(TOKEN_FILE, "w") as f:
    f.write(creds.to_json())

print(f"\nSuccess! Token saved to {TOKEN_FILE}")
print("You can now run sync_journeys.py")
