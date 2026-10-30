#!/bin/sh
# Stop Agent Office, remove it from login items and delete the app from ~/Applications.
set -eu
LABEL="local.agent-office"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -rf "$HOME/Applications/AgentOffice.app"
echo "Agent Office removed from launchd and ~/Applications."
