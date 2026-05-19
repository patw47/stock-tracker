#!/bin/bash
# Called by n8n Execute Command node
# Reads briefings from /tmp/warren_briefings.txt
# Returns openclaw agent JSON response
OPENCLAW_CONFIG_PATH=/home/warren/.openclaw/openclaw.json \
openclaw agent \
  --agent warren \
  --session-id "n8n-$(date +%Y%m%d)" \
  --message "$(cat /tmp/warren_briefings.txt)" \
  --json \
  --timeout 180 \
  2>/dev/null
