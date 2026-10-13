#!/usr/bin/env bash
# Stellt die BHZ-Oberflaeche (VNC) per noVNC als Webseite bereit.
# Standard: nur von diesem Rechner erreichbar. Fuer Zugriff aus dem LAN
# (z.B. Home-Assistant-Dashboard): LISTEN=0.0.0.0:6080 ./run-web.sh
set -euo pipefail
# Projektordner ist der, in dem novnc/ und .venv/ liegen: entweder der
# Ordner des Skripts selbst oder der darueber.
ROOT="$(cd "$(dirname "$0")" && pwd)"
[ -d "$ROOT/novnc" ] || ROOT="$(dirname "$ROOT")"
cd "$ROOT"

if [ ! -x .venv/bin/websockify ] || [ ! -d novnc ]; then
    echo "Fehler: in $ROOT fehlt .venv/bin/websockify oder novnc/" >&2
    exit 1
fi

# Eigene Startseite (nur ansehen / bedienen) in das noVNC-Verzeichnis legen
[ -f web/index.html ] && cp web/index.html novnc/index.html

BHZ_HOST="${BHZ_HOST:-192.168.178.20}"
BHZ_PORT="${BHZ_PORT:-5900}"
LISTEN="${LISTEN:-127.0.0.1:6080}"

exec .venv/bin/websockify --web novnc "$LISTEN" "$BHZ_HOST:$BHZ_PORT"
