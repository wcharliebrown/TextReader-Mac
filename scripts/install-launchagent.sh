#!/bin/bash
# Install the server as a LaunchAgent so it is running whenever Chrome needs it.
set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.textreader.api"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

LOG="$HOME/Library/Logs/TextReaderAPI/server.log"
# 127.0.0.1 keeps the server private to this Mac. TEXTREADER_HOST=0.0.0.0 lets
# other machines on the LAN use it too; there is no authentication.
HOST="${TEXTREADER_HOST:-127.0.0.1}"

mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$LOG")"
sed -e "s|__PROJECT__|$PROJECT|g" -e "s|__LOG__|$LOG|g" -e "s|__HOST__|$HOST|g" \
  "$PROJECT/scripts/com.textreader.api.plist" > "$TARGET"

# bootout is expected to fail when nothing is loaded yet.
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
# bootout returns before the old instance is gone, and bootstrapping over it
# fails with "5: Input/output error", so retry briefly.
for _ in 1 2 3 4 5; do
  launchctl bootstrap "gui/$UID" "$TARGET" 2>/dev/null && break
  sleep 1
done
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
