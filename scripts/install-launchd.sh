#!/bin/sh
# Build the menubar app, put it in ~/Applications and start it at login (macOS, launchd).
# The app starts the server itself. Usage: scripts/install-launchd.sh [port]
set -eu

PORT="${1:-8767}"
LABEL="local.agent-office"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/Applications/AgentOffice.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/agent-office.log"

"$ROOT/scripts/build-menubar.sh" "$PORT"
mkdir -p "$HOME/Applications" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
rm -rf "$APP"
# move, not copy: a second Agent Office left in build/ shows up in Spotlight and Launchpad
mv "$ROOT/build/AgentOffice.app" "$APP"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$APP/Contents/MacOS/AgentOffice</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$PATH</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST

# the old job (server only, same label) and a manually opened copy of the app give way
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
pkill -x AgentOffice 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Agent Office is in the menu bar: http://localhost:$PORT (log: $LOG)"
