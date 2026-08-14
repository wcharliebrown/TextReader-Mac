#!/bin/bash
# Install the server as a LaunchAgent so it is running whenever Chrome needs it.
set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.textreader.api"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

LOG="$HOME/Library/Logs/TextReaderAPI/server.log"

mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$LOG")"
sed -e "s|__PROJECT__|$PROJECT|g" -e "s|__LOG__|$LOG|g" \
  "$PROJECT/scripts/com.textreader.api.plist" > "$TARGET"

# bootout is expected to fail when nothing is loaded yet.
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$TARGET"
launchctl kickstart -k "gui/$UID/$LABEL"

echo "installed $TARGET"
echo -n "waiting for the server to answer"
for _ in $(seq 1 60); do
  if curl -sf http://127.0.0.1:8842/healthz >/dev/null; then
    echo " - ok"
    curl -s http://127.0.0.1:8842/healthz
    echo
    exit 0
  fi
  echo -n "."
  sleep 1
done
echo " - timed out; check $LOG"
exit 1
