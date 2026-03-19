#!/usr/bin/env python3
"""Re-authenticate Gmail OAuth. Run this whenever the Gmail token expires."""
from fetchers.gmail_fetcher import GmailFetcher

print("Opening browser for Gmail OAuth authorization...")
GmailFetcher()
print("✓ Gmail token saved to credentials/token.json")
print("You can now run main.py or click Refresh in the job board.")
