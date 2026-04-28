#!/bin/bash
cd ~/ai-trading-agent
source venv/bin/activate
set -a; source ~/ai-trading-agent/.env; set +a

# Apply patches
python patch_agent.py

# Clear ALL Python cache
find . -name "*.pyc" -delete
find . -name "__pycache__" -type d -exec rm -rf {} +

# Stop bot completely
launchctl unload ~/Library/LaunchAgents/com.trading.bot.plist
sleep 5

# Verify agent loads clean
python -c "from agent import run_analysis_telegram; print('✅ Agent OK')" || { echo "❌ Agent failed to load"; exit 1; }

# Restart
launchctl load ~/Library/LaunchAgents/com.trading.bot.plist
sleep 3
echo "✅ Deployed and restarted"
tail -5 bot.log

# Start watchdog
pkill -f watchdog.py 2>/dev/null
sleep 1
nohup python3 watchdog.py >> watchdog.log 2>&1 &
echo "✅ Watchdog started"
