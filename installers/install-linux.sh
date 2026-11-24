#!/usr/bin/env bash
# Instala o actualiza SaveSync en Steam Deck / Linux (x86_64).
#   curl -fsSL https://github.com/__REPO__/releases/latest/download/install-linux.sh | bash
set -euo pipefail

REPO="${SAVESYNC_REPO:-__REPO__}"
DIR="${SAVESYNC_DIR:-$HOME/savesync}"
BASE="https://github.com/$REPO/releases/latest/download"

if [ "$(uname -m)" != "x86_64" ]; then
  echo "SaveSync para Linux está compilado para x86_64 (tu equipo: $(uname -m))." >&2
  exit 1
fi

echo "==> Descargando SaveSync"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fL --progress-bar -o "$TMP/app.tar.gz" "$BASE/SaveSync-linux-x86_64.tar.gz"
curl -fL --progress-bar -o "$TMP/savesync-cli" "$BASE/savesync-cli-linux-x86_64"

echo "==> Instalando en $DIR"
mkdir -p "$DIR"
rm -rf "$DIR/app.new"
mkdir -p "$DIR/app.new"
tar -xzf "$TMP/app.tar.gz" -C "$DIR/app.new"
rm -rf "$DIR/app"
mv "$DIR/app.new" "$DIR/app"
install -m 755 "$TMP/savesync-cli" "$DIR/savesync-cli"
chmod +x "$DIR/app/SaveSync"

# Acceso en el menú de aplicaciones (y en el escritorio)
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
cat > "$APPS/savesync.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=SaveSync
Comment=Sincroniza tus partidas guardadas con GitHub
Exec=$DIR/app/SaveSync
Icon=$DIR/app/icon.png
Terminal=false
Categories=Game;Utility;
StartupWMClass=SaveSync
DESK
chmod +x "$APPS/savesync.desktop"
if [ -d "$HOME/Desktop" ]; then
  cp -f "$APPS/savesync.desktop" "$HOME/Desktop/savesync.desktop"
  chmod +x "$HOME/Desktop/savesync.desktop"
fi
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" >/dev/null 2>&1 || true

echo
echo "==> Listo: $("$DIR/savesync-cli" --version 2>/dev/null || echo SaveSync)"
echo
echo "  Abrir la app      : menú de aplicaciones > SaveSync"
echo "  En modo juego     : en el menú, clic derecho en SaveSync > 'Agregar a Steam'"
echo "  Sincronizar al jugar (Steam > juego > Propiedades > Opciones de lanzamiento):"
echo "      $DIR/savesync-cli run elden-ring -- %command%"
echo
echo "  Para actualizar, vuelve a ejecutar este mismo comando."
