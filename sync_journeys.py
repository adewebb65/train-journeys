#!/usr/bin/env python3
"""
Hourly sync: checks Gmail for new Trainline confirmation emails and updates data.json.
"""

import json
import os
import base64
import subprocess
from datetime import datetime, timezone

import anthropic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, "data.json")
CREDENTIALS_FILE = os.path.join(SCRIPT_DIR, "credentials.json")
TOKEN_FILE = os.path.join(SCRIPT_DIR, "token.json")
TRAINLINE_SENDER = "auto-confirm@info.thetrainline.com"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_message_body(msg):
    """Extract plain text or HTML body from a Gmail message."""
    payload = msg.get("payload", {})

    def decode_part(part):
        data = part.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        return ""

    def find_body(payload):
        mime = payload.get("mimeType", "")
        if mime == "text/html":
            return decode_part(payload)
        if mime == "text/plain":
            return decode_part(payload)
        for part in payload.get("parts", []):
            result = find_body(part)
            if result:
                return result
        return ""

    return find_body(payload)


def parse_email_with_claude(email_body, message_id, email_date):
    """Use Claude to extract structured journey data from a Trainline email."""
    client = anthropic.Anthropic()

    prompt = f"""You are parsing a Trainline booking confirmation email. Extract all journey data and return it as a single JSON object.

The JSON must follow this exact schema (use null for any field you cannot find):

{{
  "id": "{message_id}",
  "type": "single" or "return",
  "bookingRef": "string",
  "emailDate": "{email_date}",
  "calendarEventCreated": false,
  "calendarEventIds": [],
  "outbound": {{
    "isoDate": "YYYY-MM-DD",
    "dateDisplay": "Day DD Month YYYY",
    "departure": "HH:MM",
    "arrival": "HH:MM",
    "duration": "Xh Ym",
    "changes": 0,
    "from": "Station Name",
    "to": "Station Name",
    "legs": [
      {{
        "departure": "HH:MM",
        "from": "Station Name",
        "to": "Station Name",
        "operator": "string",
        "ticketType": "string",
        "arrival": "HH:MM",
        "changeMinutes": 0
      }}
    ]
  }},
  "return": null,
  "cost": {{
    "items": [
      {{"description": "string", "price": 0.00}}
    ],
    "bookingFee": 0.00,
    "total": 0.00
  }}
}}

Notes:
- For single journeys, set "return" to null
- "changes" is the number of interchanges (legs - 1)
- "changeMinutes" on a leg is the wait time before the NEXT leg (omit on the last leg)
- All prices are numbers (not strings)
- dateDisplay format example: "Tuesday 21 April 2026"

Email content:
{email_body[:15000]}

Return only the JSON object, no explanation."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    text = message.content[0].text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def load_data():
    with open(DATA_FILE, "r") as f:
        return json.load(f)


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, separators=(",", ":"))
        f.write("\n")


def git_commit_and_push():
    cmds = [
        ["git", "-C", SCRIPT_DIR, "add", "data.json"],
        ["git", "-C", SCRIPT_DIR, "commit", "-m", "Update journey data in data.json"],
        ["git", "-C", SCRIPT_DIR, "push", "-u", "origin", "main"],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Git command failed: {' '.join(cmd)}\n{result.stderr}")
            return False
    return True


def main():
    data = load_data()
    history_id = data.get("lastProcessedHistoryId")
    processed_ids = set(data.get("processedMessageIds", []))

    service = get_gmail_service()
    new_journeys = []
    new_message_ids = []
    latest_history_id = history_id

    if history_id:
        try:
            response = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
        except Exception as e:
            print(f"History API error: {e}")
            return

        latest_history_id = response.get("historyId", history_id)
        message_ids = []
        for record in response.get("history", []):
            for added in record.get("messagesAdded", []):
                msg_id = added["message"]["id"]
                if msg_id not in processed_ids:
                    message_ids.append(msg_id)
    else:
        # First run: search for existing Trainline emails
        response = (
            service.users()
            .messages()
            .list(userId="me", q=f"from:{TRAINLINE_SENDER}")
            .execute()
        )
        latest_history_id = (
            service.users().getProfile(userId="me").execute().get("historyId")
        )
        message_ids = [
            m["id"]
            for m in response.get("messages", [])
            if m["id"] not in processed_ids
        ]

    for msg_id in message_ids:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_id, format="full")
            .execute()
        )

        # Check sender
        headers = msg.get("payload", {}).get("headers", [])
        sender = next((h["value"] for h in headers if h["name"].lower() == "from"), "")
        if TRAINLINE_SENDER not in sender:
            continue

        # Get email date
        date_header = next(
            (h["value"] for h in headers if h["name"].lower() == "date"), ""
        )
        from email.utils import parsedate_to_datetime
        try:
            email_date = parsedate_to_datetime(date_header).strftime("%Y-%m-%d")
        except Exception:
            email_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        body = get_message_body(msg)
        if not body:
            print(f"No body found for message {msg_id}, skipping.")
            continue

        print(f"Parsing email {msg_id} from {email_date}...")
        try:
            journey = parse_email_with_claude(body, msg_id, email_date)
            new_journeys.append(journey)
            new_message_ids.append(msg_id)
        except Exception as e:
            print(f"Failed to parse email {msg_id}: {e}")

    if new_journeys:
        data["journeys"].extend(new_journeys)
        data["processedMessageIds"].extend(new_message_ids)
        data["lastProcessedHistoryId"] = latest_history_id
        data["lastUpdated"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"
        save_data(data)
        print(f"Added {len(new_journeys)} journey(s). Committing...")
        git_commit_and_push()
    else:
        print("No new Trainline emails found.")
        # Still update the history ID so we don't re-scan old history
        if latest_history_id != history_id:
            data["lastProcessedHistoryId"] = latest_history_id
            save_data(data)


if __name__ == "__main__":
    main()
