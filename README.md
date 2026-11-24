# SaveSync — tus partidas guardadas en GitHub

Herramienta de línea de comandos para respaldar y sincronizar partidas guardadas entre
**Windows**, **Linux / Steam Deck** y **Android (Termux)** usando un repositorio privado de GitHub.

- Un solo archivo (`savesync.py`), sin dependencias: solo **Python 3.8+** y **git**.
- Sincronización inteligente: sabe si la versión nueva está en tu PC o en GitHub, y detecta **conflictos**
  (jugaste en los dos dispositivos sin sincronizar) en vez de pisar una partida.
- **Backup local automático** antes de sobrescribir cualquier partida (guarda los últimos 10).
- **Historial completo** en git: puedes volver a cualquier versión anterior.
- Comando `run` para que la Steam Deck sincronice sola al abrir y cerrar el juego.
- **Interfaz gráfica** en el navegador: lista de juegos por plataforma o todos juntos, autodetección
  de partidas y alta manual con explorador de carpetas.

## Interfaz gráfica

Ejecuta el programa **sin argumentos** (doble clic en `savesync.bat` en Windows) o con `gui`:

```bash
python savesync.py gui        # Windows: savesync.bat   ·   Deck/Linux: ./savesync.sh
```

Se abre una página en tu navegador (solo accesible desde tu propio equipo). Mientras la uses,
deja abierta la ventana de la terminal; ciérrala para salir.

| Pestaña            | Qué hace                                                                                     |
|--------------------|----------------------------------------------------------------------------------------------|
| **Mis partidas**   | Todos tus juegos con partida, de todos tus dispositivos. Filtra por plataforma (Windows, Steam Deck, Linux, Android, *En GitHub*), por sistema (PC, GBA, PS2…) o busca por nombre. «Todos juntos» o «Por plataforma». Clic en un juego: detalle, sincronizar, subir/bajar, historial, cambiar carpeta, quitar. |
| **Detectar**       | Busca partidas donde suelen guardarse y te deja agregarlas con un clic.                      |
| **Agregar manual** | Para lo que no se detecta: nombre, sistema, carpeta (con explorador), patrones de archivos y vista previa. |
| **Ajustes**        | Nombre del dispositivo y lista de tus dispositivos.                                          |

Cada dispositivo guarda en el repo (`devices/<nombre>.json`) la lista de juegos con partida que tiene;
así, desde la PC ves también lo que hay en la Steam Deck o en el teléfono.

### Qué detecta automáticamente

- **Juegos de PC**: usa la base de datos de [Ludusavi](https://github.com/mtkennerly/ludusavi-manifest)
  (más de 20.000 juegos, datos de PCGamingWiki). Se descarga la primera vez (~18 MB) y se actualiza cada 2 semanas.
  En Steam Deck/Linux también busca dentro de los prefijos de **Proton**, y las bibliotecas de Steam en la tarjeta SD.
- **Emuladores**: RetroArch, PCSX2, DuckStation, PPSSPP, Dolphin, Ryujinx, yuzu/Eden/Citron/Sudachi,
  Citra/Azahar, Cemu, RPCS3 y todas las carpetas de **EmuDeck** (`~/Emulation/saves`).
- En las carpetas de emuladores muestra **cada juego por separado** (ej. *Pokémon Emerald*, *Chrono Trigger*)
  y, si RetroArch ordena las partidas por núcleo, también su consola (GBA, SNES…).

Desde la terminal: `savesync scan` (ver) o `savesync scan --add` (agregar todo).

## Cómo funciona

```
 PC (Windows)                GitHub (repo privado)              Steam Deck
 %APPDATA%\EldenRing  ──push──▶  saves/elden-ring/  ◀──pull──  .../compatdata/1245620/.../EldenRing
                                 games.json  (lista de juegos y rutas por plataforma)
```

En cada dispositivo se guarda una copia del repo y un registro de la última versión sincronizada.
Con eso `sync` decide:

| Situación                                       | Qué hace                    |
|-------------------------------------------------|-----------------------------|
| Cambió solo tu partida local                    | La sube ↑                   |
| Cambió solo la de GitHub (jugaste en otro lado) | La descarga ↓ (con backup)  |
| Cambiaron las dos                               | Te pregunta cuál conservar  |

## 1. Crear tu repositorio de partidas (privado)

> No confundir con el repositorio de SaveSync (el código). Tus partidas van en **otro** repositorio, privado.

1. En GitHub crea un repositorio **privado** y vacío, por ejemplo `mis-partidas` (sin README).
2. Cada dispositivo necesita poder iniciar sesión en GitHub:
   - **Windows**: Git for Windows abre el navegador para iniciar sesión la primera vez.
   - **Steam Deck / Linux / Termux**: crea una clave SSH (`ssh-keygen -t ed25519`), agrega la pública en
     GitHub → *Settings → SSH and GPG keys* y usa la dirección `git@github.com:TU_USUARIO/mis-partidas.git`.
     Alternativa: un *fine-grained token* con permiso *Contents: Read and write* solo para ese repo.

## 2. Instalar SaveSync

Descarga desde la página de **[Releases](../../releases/latest)**:

| Plataforma | Cómo |
|---|---|
| **Windows** | Descarga `SaveSync-windows-x64.exe` y ábrelo. Necesitas [Git for Windows](https://git-scm.com/download/win). No hace falta Python. |
| **Steam Deck / Linux** | En Konsole: `curl -fsSL https://github.com/TU_USUARIO/savesync/releases/latest/download/install-linux.sh \| bash` — instala en `~/savesync` y crea el acceso en el menú. |
| **Android** | Instala **Termux desde F-Droid** y ejecuta: `curl -fsSL https://github.com/TU_USUARIO/savesync/releases/latest/download/install-termux.sh \| bash` — después escribe `savesync`. |
| **Cualquier sistema con Python 3.8+** | Descarga `SaveSync-python.zip`, descomprime y ejecuta `python savesync.py`. |

Para actualizar: descarga el nuevo `.exe`, o vuelve a ejecutar el mismo comando de instalación.
La interfaz avisa cuando hay una versión nueva.

> Windows SmartScreen puede advertir que el `.exe` "no es de un editor conocido" porque no está firmado:
> pulsa *Más información → Ejecutar de todas formas*.

Después abre SaveSync, pega la dirección de tu repositorio de partidas y ponle nombre al dispositivo.

### Sincronizar al jugar en Steam Deck
Steam → juego → ⚙️ → Propiedades → Opciones de lanzamiento:
```
/home/deck/savesync/savesync run elden-ring -- %command%
```
Antes de abrir el juego baja tu última partida; al cerrarlo sube la nueva. Funciona en modo juego.

### Android: carpetas de emuladores
Desde Android 11, Termux no puede leer `Android/data/...`. Configura tus emuladores para guardar en una
carpeta normal (ej. `/storage/emulated/0/RetroArch/saves`). Con la app **Termux:Widget** puedes poner
SaveSync en la pantalla de inicio (el instalador ya crea los accesos).

## 3. Uso diario

| Comando                                   | Para qué                                                       |
|-------------------------------------------|----------------------------------------------------------------|
| `list`                                    | Juegos, carpeta en este dispositivo y estado                   |
| `status`                                  | Resumen rápido                                                 |
| `sync [juego...]`                         | Sincroniza todo (o solo los juegos indicados)                  |
| `sync --dry-run`                          | Muestra qué haría sin tocar nada                               |
| `sync --prefer local\|remote`             | Resuelve conflictos sin preguntar                              |
| `push <juego>` / `pull <juego>`           | Fuerza subir / fuerza descargar                                |
| `add <id> <carpeta> --name "Nombre"`      | Añade un juego nuevo                                           |
| `set-path <id> <carpeta>`                 | Cambia la carpeta del juego solo en este dispositivo           |
| `set-path <id> <carpeta> --shared`        | La guarda en el repo para todos los dispositivos de esa plataforma |
| `history <id>`                            | Versiones subidas a GitHub                                     |
| `restore <id> <commit>`                   | Recupera una versión antigua del historial                     |
| `backups`                                 | Backups locales automáticos                                    |
| `run <id> -- <comando>`                   | Sincroniza, abre el juego y sincroniza al cerrar               |
| `info`                                    | Configuración actual                                           |
| `gui`                                     | Abre la interfaz gráfica (también sin argumentos)              |
| `scan [--add]`                            | Detecta partidas en este dispositivo                           |
| `remove <id> [--delete-remote]`           | Quita un juego del catálogo                                    |

### Añadir un juego
```bash
# En la PC (la ruta queda guardada para Windows en games.json)
savesync add hollow-knight "%USERPROFILE%/AppData/LocalLow/Team Cherry/Hollow Knight" --name "Hollow Knight" --include "user*.dat"

# En la Steam Deck, indica dónde está ahí (Proton usa el appid del juego)
savesync set-path hollow-knight "~/.local/share/Steam/steamapps/compatdata/367520/pfx/drive_c/users/steamuser/AppData/LocalLow/Team Cherry/Hollow Knight" --shared
```
Las rutas aceptan `~`, `%VARIABLE%` y `$VARIABLE`. Con `--include` y `--exclude` filtras archivos
(patrones tipo `*.sav`, `*/ER0000.*`). [PCGamingWiki](https://www.pcgamingwiki.com) indica dónde guarda cada juego.

## Juegos incluidos de ejemplo

| id                 | Windows                    | Steam Deck                                                  | Android                                   |
|--------------------|----------------------------|-------------------------------------------------------------|-------------------------------------------|
| `elden-ring`       | `%APPDATA%\EldenRing`      | `~/.local/share/Steam/steamapps/compatdata/1245620/pfx/.../EldenRing` | —                                  |
| `retroarch-saves`  | `C:\RetroArch-Win64\saves` | `~/Emulation/saves/retroarch/saves` (EmuDeck)               | `/storage/emulated/0/RetroArch/saves`     |
| `retroarch-states` | `C:\RetroArch-Win64\states`| `~/Emulation/saves/retroarch/states` (EmuDeck)              | `/storage/emulated/0/RetroArch/states`    |

Las rutas de RetroArch dependen de cómo lo instalaste: compruébalas en
*Ajustes → Directorio* y corrígelas con `set-path`.

## Notas importantes sobre Elden Ring

- La partida (`ER0000.sl2`) está dentro de una carpeta con tu **SteamID64**. Funciona entre PC y Deck
  porque usas la **misma cuenta de Steam**; con otra cuenta el juego no la aceptará.
- **Steam Cloud** también sincroniza esta partida. Si restauras una versión antigua, Steam puede mostrar
  un aviso de conflicto: elige la versión *local*. Si prefieres que solo mande SaveSync, desactiva
  Steam Cloud para Elden Ring (Propiedades → General).
- Sincroniza **con el juego cerrado** (el comando `run` ya lo hace bien).
- Usar SaveSync como respaldo no toca el juego ni los archivos de Easy Anti-Cheat; solo copia el archivo de guardado.

## Dónde guarda sus datos SaveSync

- Windows: `%APPDATA%\savesync\`  ·  Linux/Deck/Termux: `~/.config/savesync/`
- `config.json` (URL, nombre del dispositivo, rutas propias), `state.json` (última versión sincronizada),
  `repo/` (copia del repositorio), `backups/` (copias automáticas antes de sobrescribir)
  y `ludusavi-db.json` (base de datos de rutas de juegos).
- Cambia la ubicación con la variable de entorno `SAVESYNC_HOME`.

## Archivos

| Archivo              | Qué es                                   |
|----------------------|------------------------------------------|
| `savesync.py`        | Programa principal y línea de comandos   |
| `savesync_detect.py` | Autodetección de partidas                |
| `savesync_gui.py`    | Interfaz gráfica                         |
| `savesync.bat` / `savesync.sh` | Lanzadores para usarlo con Python (Windows / Linux, Deck y Termux) |
| `installers/`        | Instaladores para Steam Deck/Linux y Termux (se publican en cada release) |
| `.github/workflows/release.yml` | Genera los ejecutables y publica la release |
| `assets/`            | Icono de la aplicación                   |

Copia siempre los tres `.py` juntos en la misma carpeta.

## Publicar una versión nueva (para quien mantiene SaveSync)

Los ejecutables los genera GitHub Actions (`.github/workflows/release.yml`):

```bash
git tag v1.2.0
git push origin v1.2.0
```

En unos minutos aparece la release con: `SaveSync-windows-x64.exe`, `SaveSync-linux-x86_64`,
`SaveSync-python.zip`, `install-linux.sh`, `install-termux.sh` y `SHA256SUMS.txt`.
El número de versión y el repositorio se toman del tag, no hace falta editar nada.
Para probar los builds sin publicar: *Actions → Release → Run workflow*.

## Límites

- GitHub rechaza archivos de más de 100 MB; SaveSync avisa si alguno pasa de 95 MB.
- Los repos crecen con cada versión. Para partidas pequeñas (casi todas) no es problema.
