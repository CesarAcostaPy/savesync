#!/data/data/com.termux/files/usr/bin/bash
# Instala o actualiza SaveSync en Android (Termux).
#   curl -fsSL https://github.com/__REPO__/releases/latest/download/install-termux.sh | bash
set -e

REPO="${SAVESYNC_REPO:-__REPO__}"
BASE="https://github.com/$REPO/releases/latest/download"
DIR="$HOME/savesync"

echo "==> Instalando dependencias (python, git, openssh)"
pkg update -y
pkg install -y python git openssh curl unzip

if [ ! -d "$HOME/storage" ]; then
  echo "==> Pidiendo permiso de almacenamiento (acepta el aviso de Android)"
  termux-setup-storage || true
  sleep 3
fi

echo "==> Descargando SaveSync"
TMP="${TMPDIR:-$PREFIX/tmp}/savesync-install"
rm -rf "$TMP" && mkdir -p "$TMP"
curl -fL --progress-bar -o "$TMP/SaveSync.zip" "$BASE/SaveSync-python.zip"
unzip -q -o "$TMP/SaveSync.zip" -d "$TMP"
mkdir -p "$DIR"
cp -f "$TMP"/SaveSync/* "$DIR/"
rm -rf "$TMP"

# comando "savesync"
cat > "$PREFIX/bin/savesync" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
exec python "$HOME/savesync/savesync.py" "$@"
EOF
chmod +x "$PREFIX/bin/savesync"

# accesos para el widget Termux:Widget (opcional)
mkdir -p "$HOME/.shortcuts"
printf '#!/data/data/com.termux/files/usr/bin/bash\nsavesync gui\n' > "$HOME/.shortcuts/SaveSync"
printf '#!/data/data/com.termux/files/usr/bin/bash\nsavesync sync\nsleep 4\n' > "$HOME/.shortcuts/SaveSync - Sincronizar"
chmod +x "$HOME/.shortcuts/"*

echo
echo "==> Listo. $(savesync info 2>/dev/null | head -1)"
echo
echo "  Abrir la interfaz : savesync"
echo "  Sincronizar       : savesync sync"
echo "  Con la app Termux:Widget puedes poner SaveSync en la pantalla de inicio."
echo "  Para actualizar, vuelve a ejecutar este mismo comando."
