#!/usr/bin/env bash
# Job Aggregator — one-time setup for a new machine
# Usage: bash setup.sh
set -e

echo ""
echo "═══════════════════════════════════════════"
echo "  Job Aggregator Setup"
echo "═══════════════════════════════════════════"
echo ""

# 1. Python venv
if [ ! -d "venv" ]; then
  echo "→ Creating Python virtual environment..."
  python3 -m venv venv
fi
echo "→ Installing dependencies..."
venv/bin/pip install -q -r requirements.txt
venv/bin/playwright install chromium --quiet 2>/dev/null || true

# 2. .env
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo ""
  echo "⚠️  Open .env and add your ANTHROPIC_API_KEY:"
  echo "   https://console.anthropic.com → API Keys → Create key"
  echo ""
  read -p "Press Enter after you've added your API key to .env…" _
fi

# 3. config.yaml
if [ ! -f "config.yaml" ]; then
  cp config.yaml.example config.yaml
  echo ""
  echo "→ config.yaml created. Edit it to set:"
  echo "   • markets (tw/jp/sg/us)"
  echo "   • job titles you're targeting"
  echo "   • your email address (for digest, optional)"
  echo ""
fi

echo ""
echo "═══════════════════════════════════════════"
echo "  ✅ Setup complete!"
echo ""
echo "  Start the web app:"
echo "    venv/bin/uvicorn server:app --port 8000 --reload"
echo "    → open http://localhost:8000"
echo ""
echo "  Then in the browser:"
echo "    1. /setup  — upload your resume + set culture preferences"
echo "    2. Run a fetch: python main.py"
echo "    3. /jobs   — browse fetched jobs"
echo "    4. /review — triage & approve"
echo "    5. /pipeline — track applications + AI-generated packages"
echo ""
echo "  Optional — Gmail job digest:"
echo "    python setup_gmail_auth.py"
echo "═══════════════════════════════════════════"
echo ""
