#!/bin/sh
# Stop Agent Office and remove it from login items.
set -eu
LABEL="local.agent-office"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Agent Office removed from launchd."
