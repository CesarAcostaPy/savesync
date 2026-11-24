# SaveSync

Sincroniza tus partidas guardadas entre **Windows**, **Steam Deck / Linux** y **Android** usando un
repositorio **privado** de tu cuenta de GitHub. Juega en la PC, sigue en la Deck o en el teléfono.

- App nativa en cada plataforma (Flet / Flutter). No necesita Git ni Python.
- **Detecta tus partidas** automáticamente: más de 20.000 juegos de PC (base de datos de
  [Ludusavi](https://github.com/mtkennerly/ludusavi-manifest) / PCGamingWiki), también dentro de Proton
  en la Steam Deck, y emuladores: RetroArch, PCSX2, DuckStation, PPSSPP, Dolphin, Ryujinx, yuzu y derivados,
  Citra/Azahar, Cemu, RPCS3 y EmuDeck.
- Ve **todos tus juegos** con partida, juntos o **por plataforma**, y en qué dispositivo está cada uno.
- Sincronización inteligente: sube o baja lo más nuevo y **avisa de conflictos** en vez de pisar una partida.
- **Copias de seguridad** locales automáticas e **historial** completo en GitHub.
- En la Steam Deck puede sincronizar **solo al abrir y cerrar cada juego**.

## Instalar

Descargas en **[Releases](https://github.com/CesarAcostaPy/savesync/releases/latest)**.

| Plataforma | Cómo |
|---|---|
| **Windows** | En PowerShell: `irm https://github.com/CesarAcostaPy/savesync/releases/latest/download/install-windows.ps1 \| iex` <br>Crea accesos en el menú Inicio y el escritorio. También puedes descargar `SaveSync-windows-x64.zip` y abrir `SaveSync.exe`. |
| **Steam Deck / Linux** | En Konsole (modo escritorio): `curl -fsSL https://github.com/CesarAcostaPy/savesync/releases/latest/download/install-linux.sh \| bash` <br>Instala en `~/savesync` y agrega SaveSync al menú. |
| **Android** | Descarga `SaveSync-android.apk` en el teléfono y ábrelo (acepta instalar apps de origen desconocido). |

Para actualizar, vuelve a ejecutar el mismo comando o instala el APK nuevo. La app avisa cuando hay versión nueva.

> Windows SmartScreen puede advertir que la app "no es de un editor conocido" porque no está firmada:
> *Más información → Ejecutar de todas formas*.

## Primer uso (en cada dispositivo)

1. **Conecta tu cuenta de GitHub**
   - Botón **Conectar con GitHub**: te muestra un código; ábrelo en github.com/login/device y autoriza.
   - O pega un **token**: GitHub → Settings → Developer settings → Personal access tokens.
     *Fine-grained*: solo tu repositorio de partidas con permiso *Contents: Read and write*.
     *Classic* con permiso `repo` si quieres que SaveSync cree el repositorio por ti.
2. **Elige el repositorio**: la primera vez pulsa **Crear privado** (`mis-partidas`). En los demás
   dispositivos elige ese mismo. Ponle un nombre a cada dispositivo (pc, steamdeck, telefono…).
3. **Detectar → Buscar partidas → Agregar seleccionados.** Lo que no se detecte, agrégalo en **Agregar**.
4. **Sincronizar todo** antes de cambiar de dispositivo.

## Steam Deck

- **Modo escritorio**: abre SaveSync desde el menú.
- **Modo juego**: en el menú de aplicaciones, clic derecho en SaveSync → *Agregar a Steam*.
- **Sincronizar sola al jugar**: abre el juego en SaveSync, copia la línea de **Sincronizar al jugar (Steam)**
  y pégala en Steam → juego → Propiedades → Opciones de lanzamiento. Es algo así:
  ```
  /home/deck/savesync/savesync-cli run elden-ring -- %command%
  ```
  Antes de abrir el juego baja tu última partida; al cerrarlo sube la nueva.
- El juego debe haberse abierto **una vez** en la Deck para que Proton cree su carpeta de partidas.

## Android

- La primera vez, SaveSync pide **Acceso a todos los archivos** para leer las carpetas de tus emuladores.
- Android 11+ **no permite** a ninguna app leer `Android/data/...`. Configura tus emuladores para guardar en
  una carpeta normal, por ejemplo en RetroArch: *Ajustes → Directorio → Partidas guardadas* →
  `/storage/emulated/0/RetroArch/saves`.
- La detección de juegos de PC no aplica en Android; la de emuladores sí.

## Cómo funciona

```
 PC (Windows)                  GitHub (repo privado)                   Steam Deck
 %APPDATA%\EldenRing  ──sube──▶  saves/elden-ring/data/…  ◀──baja──  …/compatdata/1245620/…/EldenRing
                                 saves/<juego>/manifest.json   (quién y cuándo)
                                 games.json                    (juegos y carpetas por plataforma)
                                 devices/<dispositivo>.json    (qué partidas tiene cada uno)
```

Cada dispositivo recuerda qué versión sincronizó por última vez. Con eso decide:

| Situación | Qué hace |
|---|---|
| Cambió solo aquí | La sube |
| Cambió solo en GitHub (jugaste en otro lado) | La baja (antes guarda una copia de seguridad) |
| Cambió en los dos | Te pregunta cuál conservar; la otra queda en el historial |

Datos locales de SaveSync: `%APPDATA%\savesync` (Windows), `~/.config/savesync` (Linux/Deck),
almacenamiento interno de la app (Android). Ahí están la configuración, la sesión de GitHub,
las copias de seguridad y la caché.

### Elden Ring

- La partida está en una carpeta con tu **SteamID**: usa la misma cuenta de Steam en todos los dispositivos.
- Steam Cloud también sincroniza este juego. Si Steam muestra un conflicto después de que SaveSync cambió la
  partida, elige la versión **local**, o desactiva Steam Cloud para el juego si prefieres que mande SaveSync.

## Línea de comandos

`savesync-cli` (incluido en los instaladores de Windows y Linux):

| Comando | Para qué |
|---|---|
| `login` · `repo` | Conectar cuenta · elegir/crear repositorio |
| `list` · `status` | Juegos y estado |
| `sync` · `push <id>` · `pull <id>` | Sincronizar · forzar subir · forzar bajar |
| `run <id> -- <comando>` | Sincronizar, abrir el juego y sincronizar al cerrar |
| `scan [--add]` · `add` · `set-path` · `remove` | Detectar · agregar · cambiar carpeta · quitar |
| `history <id>` · `restore <id> <versión>` · `backups` | Versiones en GitHub · restaurar · copias locales |

## Para quien mantiene SaveSync

```
src/
  main.py               entrada de la app (Flet)
  savesync_app.py       interfaz
  savesync_core.py      sincronización vía API de GitHub (sin git)
  savesync_detect.py    autodetección (Ludusavi + emuladores)
  savesync_cli.py       línea de comandos
  assets/icon.png
tests/mock_github.py    imitación de la API de GitHub para pruebas
installers/             instaladores de Windows y Linux
pyproject.toml          configuración de compilación de Flet
.github/workflows/release.yml
```

**Publicar una versión**: `git tag v2.0.0 && git push origin v2.0.0`. GitHub Actions compila la app para
Windows, Linux y Android, la línea de comandos para Windows y Linux, y publica la release.

**Probar en tu PC**: `pip install "flet[all]==1.1.3"` y `flet run src/main.py`.

**Opcional, una sola vez** (Settings → Secrets and variables → Actions):

- **Botón «Conectar con GitHub»**: crea una OAuth App en GitHub → Settings → Developer settings → OAuth Apps
  (Homepage y Callback: la URL de este repositorio) y marca **Enable Device Flow**. Copia su *Client ID*
  en una **variable** llamada `OAUTH_CLIENT_ID`. Sin ella, la app ofrece solo el inicio de sesión con token.
- **Firma estable del APK** (para actualizar sin desinstalar): crea un keystore
  (`keytool -genkey -v -keystore savesync.jks -keyalg RSA -keysize 2048 -validity 10000 -alias savesync`) y
  guarda como **secrets** `ANDROID_KEYSTORE_BASE64` (el archivo en base64), `ANDROID_KEYSTORE_PASSWORD`,
  `ANDROID_KEY_ALIAS` y `ANDROID_KEY_PASSWORD`. Nunca subas el `.jks` al repositorio.
