#!/usr/bin/with-contenv bash
echo "[beets-agent] Starting Beets Control Agent under S6 supervision..."
exec /lsiopy/bin/python3 /opt/beets-web-manager-agent/beets_control_agent.py
