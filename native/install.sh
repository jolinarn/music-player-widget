#!/bin/sh
# Registers native/host.py with Brave so the extension can reach the widget.
# Run once (and again if this folder moves). Then in Brave:
#   brave://extensions -> Developer mode -> Load unpacked -> <this repo>/extension
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
DEST="$HOME/.config/BraveSoftware/Brave-Browser/NativeMessagingHosts"
mkdir -p "$DEST"
chmod +x "$DIR/host.py"
cat > "$DEST/com.jolinarn.musicplayerwidget.json" <<JSON
{
  "name": "com.jolinarn.musicplayerwidget",
  "description": "YouTube Music bridge for music-player-widget",
  "path": "$DIR/host.py",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://fijoiiligkknjgndpopmlccnagfponfe/"]
}
JSON
echo "Installed $DEST/com.jolinarn.musicplayerwidget.json"
