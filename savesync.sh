#!/usr/bin/env bash
# Lanzador para Linux, Steam Deck y Termux
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/savesync.py" "$@"
