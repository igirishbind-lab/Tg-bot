cat > start.sh <<'SH'
#!/bin/bash
# install deps and run the bot
pip install -r requirements.txt
python advanced_bot_full.py
SH
chmod +x start.sh
