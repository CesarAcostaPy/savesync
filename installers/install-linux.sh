#!/usr/bin/env bash
# Instala o actualiza SaveSync en Steam Deck / Linux (x86_64).
#   curl -fsSL https://github.com/__REPO__/releases/latest/download/install-linux.sh | bash
set -euo pipefail

REPO="${SAVESYNC_REPO:-__REPO__}"
DIR="${SAVESYNC_DIR:-$HOME/savesync}"
BASE="https://github.com/$REPO/releases/latest/download"

echo "==> Instalando SaveSync en $DIR"
if ! command -v git >/dev/null 2>&1; then
  echo "AVISO: no se encontró git. SaveSync lo necesita (en SteamOS ya viene instalado)."
fi
if [ "$(uname -m)" != "x86_64" ]; then
  echo "Este ejecutable es para x86_64. En tu equipo ($(uname -m)) usa SaveSync-python.zip con Python." >&2
  exit 1
fi

mkdir -p "$DIR"
curl -fL --progress-bar -o "$DIR/savesync.new" "$BASE/SaveSync-linux-x86_64"
chmod +x "$DIR/savesync.new"
mv -f "$DIR/savesync.new" "$DIR/savesync"
curl -fsSL -o "$DIR/icon.png" "$BASE/icon.png" || true

# Acceso directo en el menú de aplicaciones (modo escritorio)
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
cat > "$APPS/savesync.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=SaveSync
Comment=Sincroniza tus partidas guardadas con GitHub
Exec=$DIR/savesync gui
Icon=$DIR/icon.png
Terminal=true
Categories=Game;Utility;
EOF
chmod +x "$APPS/savesync.desktop"
if [ -d "$HOME/Desktop" ]; then
  cp -f "$APPS/savesync.desktop" "$HOME/Desktop/savesync.desktop"
  chmod +x "$HOME/Desktop/savesync.desktop"
fi
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" >/dev/null 2>&1 || true

echo
echo "==> Listo. SaveSync $("$DIR/savesync" info 2>/dev/null | sed -n 's/^SaveSync //p')"
echo
echo "  Abrir la interfaz : busca 'SaveSync' en el menú, o ejecuta  $DIR/savesync"
echo "  Sincronizar       : $DIR/savesync sync"
echo
echo "  Sincronizar al jugar (Steam > juego > Propiedades > Opciones de lanzamiento):"
echo "    $DIR/savesync run elden-ring -- %command%"
echo
echo "  Para actualizar, vuelve a ejecutar este mismo comando."
