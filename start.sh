#!/bin/bash
set -e
set -x

echo "---- START.SH: begin ----"
echo "Working dir: $(pwd)"
echo "Listing files:"
ls -la

echo "Python version:"
python --version || python3 --version || echo "no python binary found"

echo "PIP version and installed packages (top):"
pip --version || pip3 --version || true
pip list --format=columns | sed -n '1,80p' || true

echo "Contents of requirements.txt:"
sed -n '1,200p' requirements.txt || true

echo "ENV check (show TELEGRAM_BOT_TOKEN exists):"
if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
  echo "ERROR: TELEGRAM_BOT_TOKEN is NOT set"
else
  echo "TELEGRAM_BOT_TOKEN is set (won't print for security)"
fi

echo "Installing deps..."
pip install --upgrade pip setuptools wheel || true
pip install -r requirements.txt --no-cache-dir || (echo "pip install failed"; exit 3)

echo "After install - pip list (top):"
pip list --format=columns | sed -n '1,80p' || true

echo "Running the bot..."
# run python in unbuffered mode so logs appear
python advanced_bot_full.py
echo "---- START.SH: end ----"
SH

chmod +x start.sh
