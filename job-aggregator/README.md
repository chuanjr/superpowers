# Job Aggregator

Daily job digest from LinkedIn, Indeed, 104, CakeResume, Yourator, Wellfound.

## Security

**Never commit these files** (already in `.gitignore`):
- `.env` — contains `ANTHROPIC_API_KEY`
- `credentials/client_secret.json` — Google OAuth client secret
- `credentials/token.json` — Google OAuth access token (auto-generated)
- `config.yaml` — contains your email address

If you accidentally commit any of these, revoke the credentials immediately:
- Anthropic: https://console.anthropic.com → API Keys → Delete
- Google: https://console.cloud.google.com → Credentials → Delete

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

2. Set up API key:
   ```bash
   cp .env.example .env
   # Edit .env and add your ANTHROPIC_API_KEY
   ```

3. Get Google OAuth credentials:
   - Go to https://console.cloud.google.com
   - Create project → Enable Gmail API → OAuth 2.0 Client ID (Desktop app)
   - Download as `credentials/client_secret.json`

4. Run interactive setup:
   ```bash
   python setup_cli.py
   ```

5. Test run:
   ```bash
   python main.py
   ```
   First run will open browser for Gmail OAuth. Token saved to `credentials/token.json`.

## Cron (daily at 8am)

```bash
crontab -e
```

Add:
```
0 8 * * * cd /path/to/job-aggregator && /usr/bin/python3 main.py >> logs/job-aggregator.log 2>&1
```

Create log dir:
```bash
mkdir -p /path/to/job-aggregator/logs
```

## Updating search criteria

```bash
python setup_cli.py
```

Or edit `config.yaml` directly — changes take effect on next run.
