import base64
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/gmail.send"]
CREDENTIALS_DIR = Path("credentials")

SENDER_MAP = {
    "linkedin_gmail": "from:(jobalerts-noreply@linkedin.com OR jobs-noreply@linkedin.com)",
    "indeed_gmail": "from:(jobalert@indeed.com OR alert@sg.indeed.com OR alert@jp.indeed.com OR alert@tw.indeed.com)",
}


def build_search_query(sources: dict, days_back: int = 1) -> str:
    parts = []
    for key, sender_query in SENDER_MAP.items():
        if sources.get(key):
            parts.append(sender_query)
    if not parts:
        return ""
    sender_part = " OR ".join(f"({p})" for p in parts)
    since = (datetime.now() - timedelta(days=days_back)).strftime("%Y/%m/%d")
    return f"({sender_part}) after:{since}"


def extract_html_body(message: dict) -> str:
    payload = message.get("payload", {})
    parts = payload.get("parts", [])
    if parts:
        for part in parts:
            if part.get("mimeType") == "text/html":
                data = part["body"].get("data", "")
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    data = payload.get("body", {}).get("data", "")
    if data:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    return ""


class GmailFetcher:
    def __init__(self, credentials_dir: Path = CREDENTIALS_DIR):
        self.credentials_dir = credentials_dir
        self.service = self._authenticate()

    def _authenticate(self):
        token_path = self.credentials_dir / "token.json"
        secret_path = self.credentials_dir / "client_secret.json"
        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
                creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json())
        return build("gmail", "v1", credentials=creds)

    def fetch_alert_messages(self, sources: dict, days_back: int = 1) -> Iterator[dict]:
        query = build_search_query(sources, days_back)
        if not query:
            return
        response = self.service.users().messages().list(userId="me", q=query).execute()
        messages = response.get("messages", [])
        for msg_ref in messages:
            msg = self.service.users().messages().get(
                userId="me", id=msg_ref["id"], format="full"
            ).execute()
            yield msg

    def send_email(self, to: str, from_: str, subject: str, html_body: str) -> None:
        import base64
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_
        msg["To"] = to
        msg.attach(MIMEText(html_body, "html"))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        self.service.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
