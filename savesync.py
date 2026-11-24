#!/usr/bin/env python3
"""
SaveSync - Sincroniza partidas guardadas de tus juegos con un repositorio de GitHub.

Funciona en Windows, Linux (incluida Steam Deck) y Android (vía Termux).
Solo necesita Python 3.8+ y git.

Uso rápido:
    python savesync.py setup --repo https://github.com/TU_USUARIO/mis-partidas.git
    python savesync.py list
    python savesync.py sync
"""
import argparse
import datetime as _dt
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

VERSION = "1.1.0"
RELEASES_REPO = ""  # "usuario/savesync"; lo rellena el workflow de GitHub al publicar
MAX_FILE_BYTES = 95 * 1024 * 1024  # GitHub rechaza archivos de más de 100 MB

# ---------------------------------------------------------------------------
# Catálogo inicial de juegos (se copia a games.json del repo la primera vez).
# Las rutas admiten ~, %VARIABLE% (Windows) y $VARIABLE.
# "include" son patrones relativos a la carpeta (por defecto: todo).
# ---------------------------------------------------------------------------
DEFAULT_GAMES = [
    {
        "id": "elden-ring",
        "name": "Elden Ring",
        "system": "PC",
        "steam_id": 1245620,
        "paths": {
            "windows": "%APPDATA%/EldenRing",
            "steamdeck": "~/.local/share/Steam/steamapps/compatdata/1245620/pfx/drive_c/users/steamuser/AppData/Roaming/EldenRing",
            "linux": "~/.local/share/Steam/steamapps/compatdata/1245620/pfx/drive_c/users/steamuser/AppData/Roaming/EldenRing",
        },
        # <SteamID64>/ER0000.sl2 (+ .bak). También cubre ER0000.co2 de Seamless Coop.
        "include": ["*/ER0000.*"],
        "notes": "La carpeta interna lleva tu SteamID; usa la misma cuenta de Steam en todos los dispositivos.",
    },
    {
        "id": "retroarch-saves",
        "name": "RetroArch (partidas .srm)",
        "system": "Emulador",
        "type": "collection",
        "paths": {
            "windows": "C:/RetroArch-Win64/saves",
            "steamdeck": "~/Emulation/saves/retroarch/saves",
            "linux": "~/.config/retroarch/saves",
            "android": "/storage/emulated/0/RetroArch/saves",
        },
        "include": ["*"],
        "notes": "Verifica la carpeta en RetroArch > Ajustes > Directorio > Partidas guardadas.",
    },
    {
        "id": "retroarch-states",
        "name": "RetroArch (estados guardados)",
        "system": "Emulador",
        "type": "collection",
        "paths": {
            "windows": "C:/RetroArch-Win64/states",
            "steamdeck": "~/Emulation/saves/retroarch/states",
            "linux": "~/.config/retroarch/states",
            "android": "/storage/emulated/0/RetroArch/states",
        },
        "include": ["*"],
        "notes": "Verifica la carpeta en RetroArch > Ajustes > Directorio > Estados guardados.",
    },
]

PLATFORM_FALLBACKS = {
    "windows": ["windows"],
    "steamdeck": ["steamdeck", "linux"],
    "linux": ["linux"],
    "android": ["android"],
    "macos": ["macos"],
}


# ---------------------------------------------------------------------------
# Utilidades generales
# ---------------------------------------------------------------------------
class SaveSyncError(Exception):
    pass


def info(msg):
    print(msg)


def warn(msg):
    print("⚠  " + msg, file=sys.stderr)


def now_iso():
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def stamp():
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def child_env():
    """Entorno para procesos hijos (git, el juego). En los ejecutables de PyInstaller
    se restaura LD_LIBRARY_PATH original para no contaminar git ni los juegos de Steam."""
    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
            orig = env.pop(var + "_ORIG", None)
            if orig is not None:
                env[var] = orig
            else:
                env.pop(var, None)
    return env


def detect_platform():
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if "ANDROID_ROOT" in os.environ or "com.termux" in os.environ.get("PREFIX", ""):
        return "android"
    try:
        osrel = Path("/etc/os-release").read_text(encoding="utf-8", errors="ignore").lower()
        if "steamos" in osrel:
            return "steamdeck"
    except OSError:
        pass
    return "linux"


def data_dir():
    override = os.environ.get("SAVESYNC_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "savesync"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "savesync"


def expand_path(raw):
    if not raw:
        return None
    s = re.sub(r"%([^%]+)%", lambda m: os.environ.get(m.group(1), m.group(0)), raw)
    s = os.path.expandvars(os.path.expanduser(s))
    return Path(s)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def matches(rel, include, exclude):
    ok = any(fnmatch.fnmatch(rel, p) for p in include)
    return ok and not any(fnmatch.fnmatch(rel, p) for p in exclude)


def collect_files(root, include=None, exclude=None):
    """Devuelve {ruta_relativa_posix: Path} de los archivos que coinciden."""
    include = include or ["*"]
    exclude = exclude or []
    out = {}
    root = Path(root)
    if not root.is_dir():
        return out
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            if matches(rel, include, exclude):
                out[rel] = full
    return out


def fingerprint(files):
    """Hash conjunto de un grupo de archivos (None si no hay archivos)."""
    if not files:
        return None, {}
    h = hashlib.sha256()
    detail = {}
    for rel in sorted(files):
        fh = sha256_file(files[rel])
        detail[rel] = {"sha256": fh, "size": files[rel].stat().st_size}
        h.update(rel.encode("utf-8") + b"\0" + fh.encode() + b"\n")
    return h.hexdigest(), detail


def copy_set(files, dest_root):
    dest_root = Path(dest_root)
    for rel, src in files.items():
        dst = dest_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def remove_empty_dirs(root):
    root = Path(root)
    if not root.is_dir():
        return
    for dirpath, _dirs, _files in sorted(os.walk(root), key=lambda t: -len(t[0])):
        p = Path(dirpath)
        if p != root:
            try:
                p.rmdir()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Colecciones (carpetas de emuladores con muchas partidas dentro)
# ---------------------------------------------------------------------------
# Nombre de subcarpeta (núcleo de RetroArch o emulador) -> sistema
CORE_SYSTEMS = {
    "mgba": "GBA", "gpsp": "GBA", "vba-m": "GBA", "vbam": "GBA", "vba next": "GBA", "meteor": "GBA",
    "gambatte": "GB/GBC", "sameboy": "GB/GBC", "gearboy": "GB/GBC", "tgb dual": "GB/GBC",
    "snes9x": "SNES", "snes9x 2010": "SNES", "bsnes": "SNES", "bsnes-hd beta": "SNES", "mesen-s": "SNES",
    "mesen": "NES", "nestopia": "NES", "fceumm": "NES", "quicknes": "NES",
    "genesis plus gx": "Mega Drive", "genesis plus gx wide": "Mega Drive", "picodrive": "Mega Drive",
    "beetle psx": "PS1", "beetle psx hw": "PS1", "swanstation": "PS1", "pcsx-rearmed": "PS1",
    "pcsx rearmed": "PS1", "duckstation": "PS1",
    "ppsspp": "PSP", "mupen64plus-next": "N64", "parallel n64": "N64",
    "melonds": "NDS", "melonds ds": "NDS", "desmume": "NDS",
    "dolphin": "GameCube/Wii", "flycast": "Dreamcast", "citra": "3DS",
    "pcsx2": "PS2", "lrps2": "PS2", "beetle saturn": "Saturn", "yabause": "Saturn",
    "mame": "Arcade", "fbneo": "Arcade", "final burn neo": "Arcade",
}

_SAVE_SUFFIX = re.compile(
    r"\.(srm|sav|sv|eep|fla|mpk|sra|rtc|dsv|gci|sav\.bak|bak|png|auto|state\d*|state|ps2|mcd|mcr|mc|dat|bin)$",
    re.IGNORECASE)


def clean_item_name(name):
    prev = None
    while prev != name:
        prev = name
        name = _SAVE_SUFFIX.sub("", name)
    return name.strip() or prev


def collection_items(root, game, files=None):
    """Lista los juegos individuales que hay dentro de una colección."""
    if files is None:
        files = collect_files(root, game.get("include"), game.get("exclude"))
    mode = game.get("item_mode", "files")
    default_sys = game.get("system", "")
    items = {}
    for rel in files:
        parts = rel.split("/")
        system = default_sys
        if len(parts) > 1 and parts[0].lower() in CORE_SYSTEMS:
            system = CORE_SYSTEMS[parts[0].lower()]
            parts = parts[1:]
        if mode == "dirs" and len(parts) > 1:
            name = parts[0]
        else:
            name = clean_item_name(parts[-1])
        if name.lower() in ("desktop.ini", ".ds_store", "thumbs.db"):
            continue
        items.setdefault((name, system), 0)
        items[(name, system)] += 1
    return [{"name": n, "system": s} for (n, s) in sorted(items, key=lambda k: k[0].lower())][:1000]


# ---------------------------------------------------------------------------
# Steam
# ---------------------------------------------------------------------------
def steam_roots(plat=None):
    plat = plat or detect_platform()
    roots = []
    if plat == "windows":
        try:
            import winreg  # type: ignore
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                roots.append(Path(winreg.QueryValueEx(k, "SteamPath")[0]))
        except Exception:
            pass
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            if os.environ.get(env):
                roots.append(Path(os.environ[env]) / "Steam")
    elif plat in ("linux", "steamdeck"):
        home = Path.home()
        roots += [home / ".local/share/Steam", home / ".steam/steam", home / ".steam/root",
                  home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
    out = []
    for r in roots:
        try:
            rr = r.resolve()
        except OSError:
            continue
        if rr.is_dir() and rr not in out:
            out.append(rr)
    return out


def steam_libraries(plat=None):
    libs = []
    for root in steam_roots(plat):
        if root not in libs:
            libs.append(root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            text = vdf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            p = Path(m.group(1).replace("\\\\", "\\"))
            try:
                p = p.resolve()
            except OSError:
                pass
            if p.is_dir() and p not in libs:
                libs.append(p)
    return libs


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:60] or "juego"


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------
class Git:
    def __init__(self, repo_dir):
        self.dir = Path(repo_dir)

    def run(self, *args, check=True, capture=True):
        cmd = ["git", "-C", str(self.dir)] + list(args)
        res = subprocess.run(cmd, capture_output=capture, text=True, env=child_env(),
                             encoding="utf-8", errors="replace")
        if check and res.returncode != 0:
            raise SaveSyncError("git {} falló:\n{}".format(" ".join(args), (res.stderr or res.stdout).strip()))
        return res

    def has_commits(self):
        return self.run("rev-parse", "--verify", "HEAD", check=False).returncode == 0

    def has_remote_branch(self):
        out = self.run("ls-remote", "--heads", "origin", check=False)
        return out.returncode == 0 and out.stdout.strip() != ""

    def pull(self):
        if not self.has_remote_branch():
            return
        branch = self.branch()
        res = self.run("pull", "--rebase", "origin", branch, check=False)
        if res.returncode != 0:
            self.run("rebase", "--abort", check=False)
            raise SaveSyncError("No se pudo actualizar desde GitHub:\n" + (res.stderr or res.stdout).strip())

    def branch(self):
        res = self.run("rev-parse", "--abbrev-ref", "HEAD", check=False)
        b = res.stdout.strip()
        return b if res.returncode == 0 and b != "HEAD" else "main"

    def commit_and_push(self, message):
        self.run("add", "-A")
        if self.run("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False
        self.run("commit", "-m", message)
        branch = self.branch()
        res = self.run("push", "-u", "origin", branch, check=False)
        if res.returncode != 0:
            # Otro dispositivo subió algo mientras tanto: rebase y reintento.
            self.pull()
            self.run("push", "-u", "origin", branch)
        return True


def ensure_git():
    if shutil.which("git") is None:
        hint = {
            "windows": "Instálalo desde https://git-scm.com/download/win",
            "android": "En Termux: pkg install git",
            "steamdeck": "SteamOS ya trae git; si no, usa el modo escritorio.",
        }.get(detect_platform(), "Instálalo con el gestor de paquetes de tu sistema.")
        raise SaveSyncError("No se encontró git. " + hint)


# ---------------------------------------------------------------------------
# Núcleo
# ---------------------------------------------------------------------------
class SaveSync:
    def __init__(self):
        self.home = data_dir()
        self.config_path = self.home / "config.json"
        self.state_path = self.home / "state.json"
        self.backup_root = self.home / "backups"
        self.config = load_json(self.config_path, {})
        self.state = load_json(self.state_path, {})
        self.platform = self.config.get("platform") or detect_platform()
        self.device = self.config.get("device_name") or socket.gethostname()
        self.interactive = True

    # --- configuración -----------------------------------------------------
    @property
    def repo_dir(self):
        rd = self.config.get("repo_dir")
        if not rd:
            raise SaveSyncError("SaveSync no está configurado. Ejecuta primero: savesync setup --repo URL")
        return Path(rd)

    @property
    def git(self):
        return Git(self.repo_dir)

    def save_config(self):
        save_json(self.config_path, self.config)

    def save_state(self):
        save_json(self.state_path, self.state)

    def games_path(self):
        return self.repo_dir / "games.json"

    def load_games(self):
        data = load_json(self.games_path(), {"games": []})
        return data.get("games", [])

    def save_games(self, games):
        save_json(self.games_path(), {"version": 1, "games": games})

    def get_game(self, games, gid):
        for g in games:
            if g["id"] == gid:
                return g
        raise SaveSyncError("No existe el juego '{}'. Usa 'list' para ver los disponibles.".format(gid))

    def local_path(self, game):
        override = self.config.get("path_overrides", {}).get(game["id"])
        if override:
            return expand_path(override)
        for plat in PLATFORM_FALLBACKS.get(self.platform, [self.platform]):
            raw = game.get("paths", {}).get(plat)
            if raw:
                p = expand_path(raw)
                if not p.exists() and game.get("steam_id") and self.platform in ("linux", "steamdeck"):
                    # el juego puede estar en otra biblioteca de Steam (ej. tarjeta SD)
                    m = re.search(r"steamapps/compatdata/(\d+)/(.*)$", p.as_posix())
                    if m:
                        for lib in steam_libraries(self.platform):
                            cand = lib / "steamapps" / "compatdata" / m.group(1) / m.group(2)
                            if cand.exists():
                                return cand
                return p
        return None

    def remote_dir(self, game):
        return self.repo_dir / "saves" / game["id"]

    # --- estado ------------------------------------------------------------
    def local_files(self, game):
        p = self.local_path(game)
        if p is None:
            return None, {}
        files = collect_files(p, game.get("include"), game.get("exclude"))
        return p, files

    def remote_files(self, game):
        return collect_files(self.remote_dir(game) / "data")

    def remote_manifest(self, game):
        return load_json(self.remote_dir(game) / "manifest.json", None)

    def analyze(self, game):
        path, lfiles = self.local_files(game)
        lhash, _ = fingerprint(lfiles)
        manifest = self.remote_manifest(game)
        rhash = manifest.get("hash") if manifest else None
        base = self.state.get(game["id"], {}).get("hash")
        if path is None:
            status = "sin-ruta"
        elif lhash is None and rhash is None:
            status = "vacio"
        elif lhash == rhash:
            status = "sincronizado"
        elif rhash is None:
            status = "solo-local"
        elif lhash is None:
            status = "solo-remoto"
        elif base is None:
            status = "conflicto"
        elif lhash == base:
            status = "remoto-nuevo"
        elif rhash == base:
            status = "local-nuevo"
        else:
            status = "conflicto"
        return {
            "path": path,
            "local_files": lfiles,
            "local_hash": lhash,
            "remote_hash": rhash,
            "manifest": manifest,
            "base": base,
            "status": status,
        }

    def remember(self, game, h):
        self.state[game["id"]] = {"hash": h, "synced_at": now_iso()}
        self.save_state()

    # --- operaciones -------------------------------------------------------
    def backup_local(self, game, files):
        if not files:
            return None
        base = self.backup_root / game["id"] / stamp()
        dest, n = base, 1
        while dest.exists():
            n += 1
            dest = base.with_name("{}-{}".format(base.name, n))
        copy_set(files, dest)
        self.prune_backups(game)
        return dest

    def prune_backups(self, game, keep=None):
        keep = keep or int(self.config.get("keep_backups", 10))
        d = self.backup_root / game["id"]
        if not d.is_dir():
            return
        snaps = sorted(p for p in d.iterdir() if p.is_dir())
        for old in snaps[:-keep]:
            shutil.rmtree(old, ignore_errors=True)

    def upload(self, game, a):
        """Copia las partidas locales al repo (no hace commit)."""
        files = a["local_files"]
        too_big = [r for r, p in files.items() if p.stat().st_size > MAX_FILE_BYTES]
        if too_big:
            raise SaveSyncError("Archivos demasiado grandes para GitHub (>95 MB): " + ", ".join(too_big))
        rdir = self.remote_dir(game)
        if (rdir / "data").exists():
            shutil.rmtree(rdir / "data")
        copy_set(files, rdir / "data")
        lhash, detail = fingerprint(files)
        save_json(rdir / "manifest.json", {
            "game": game["id"],
            "hash": lhash,
            "device": self.device,
            "platform": self.platform,
            "updated_at": now_iso(),
            "files": detail,
        })
        self.remember(game, lhash)

    def download(self, game, a):
        """Reemplaza las partidas locales por las del repo (con backup previo)."""
        path = a["path"]
        rfiles = self.remote_files(game)
        bk = self.backup_local(game, a["local_files"])
        if bk:
            info("   backup local en: {}".format(bk))
        path.mkdir(parents=True, exist_ok=True)
        # borra archivos gestionados que ya no existen en el remoto
        for rel, p in a["local_files"].items():
            if rel not in rfiles:
                p.unlink()
        copy_set(rfiles, path)
        remove_empty_dirs(path)
        self.remember(game, a["remote_hash"])

    # --- reportes por dispositivo -----------------------------------------
    def report_path(self):
        return self.repo_dir / "devices" / (slugify(self.device) + ".json")

    def build_report(self, games=None):
        games = games if games is not None else self.load_games()
        out = {}
        for g in games:
            path, files = self.local_files(g)
            if not files:
                continue
            mt = max(p.stat().st_mtime for p in files.values())
            entry = {
                "files": len(files),
                "bytes": sum(p.stat().st_size for p in files.values()),
                "modified": _dt.datetime.fromtimestamp(mt).astimezone().isoformat(timespec="seconds"),
            }
            if g.get("type") == "collection":
                entry["items"] = collection_items(path, g, files)
            out[g["id"]] = entry
        return out

    def write_report(self, games=None):
        """Guarda en el repo qué partidas tiene este dispositivo. Devuelve True si cambió."""
        data = self.build_report(games)
        path = self.report_path()
        old = load_json(path, {})
        if old.get("games") == data and old.get("platform") == self.platform:
            return False
        save_json(path, {"device": self.device, "platform": self.platform,
                         "updated_at": now_iso(), "games": data})
        return True

    def load_reports(self):
        d = self.repo_dir / "devices"
        reps = []
        if d.is_dir():
            for f in sorted(d.glob("*.json")):
                r = load_json(f, None)
                if r:
                    reps.append(r)
        return reps

    # --- edición del catálogo ---------------------------------------------
    def find_existing(self, games, entry):
        for g in games:
            if g["id"] == entry.get("id"):
                return g
            if entry.get("steam_id") and g.get("steam_id") == entry.get("steam_id"):
                return g
        return None

    def add_or_merge(self, entry, path, shared=True):
        """Añade un juego al catálogo o, si ya existe, le pone la ruta de esta plataforma."""
        games = self.load_games()
        g = self.find_existing(games, entry)
        if g is None:
            g = {k: v for k, v in entry.items() if v not in (None, "", [])}
            g.setdefault("id", slugify(g.get("name", "juego")))
            base_id, n = g["id"], 1
            while any(x["id"] == g["id"] for x in games):
                n += 1
                g["id"] = "{}-{}".format(base_id, n)
            g["paths"] = {}
            g.setdefault("include", ["*"])
            games.append(g)
            created = True
        else:
            created = False
        if shared:
            g.setdefault("paths", {})[self.platform] = path
        else:
            self.config.setdefault("path_overrides", {})[g["id"]] = path
            self.save_config()
        self.save_games(games)
        return g, created

    def remove_game(self, gid, delete_remote=False):
        games = self.load_games()
        g = self.get_game(games, gid)
        games = [x for x in games if x["id"] != gid]
        self.save_games(games)
        if delete_remote and self.remote_dir(g).exists():
            shutil.rmtree(self.remote_dir(g))
        self.state.pop(gid, None)
        self.save_state()
        self.config.get("path_overrides", {}).pop(gid, None)
        self.save_config()
        return g

    def ask_conflict(self, game, a):
        m = a["manifest"] or {}
        info("   CONFLICTO: cambió aquí y también en '{}' ({})".format(m.get("device", "?"), m.get("updated_at", "?")))
        if not self.interactive or not sys.stdin.isatty():
            return "skip"
        while True:
            ans = input("   ¿Qué versión conservar? [l]ocal / [r]emota / [s]altar: ").strip().lower()
            if ans in ("l", "local"):
                return "local"
            if ans in ("r", "remota", "remote"):
                return "remote"
            if ans in ("s", "saltar", "skip", ""):
                return "skip"


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------
def cmd_setup(ss, args):
    ensure_git()
    repo_url = args.repo or ss.config.get("repo_url")
    if not repo_url:
        repo_url = input("URL del repositorio de GitHub (privado): ").strip()
    if not repo_url:
        raise SaveSyncError("Necesito la URL del repositorio.")
    repo_dir = Path(args.dir).expanduser() if args.dir else Path(ss.config.get("repo_dir") or ss.home / "repo")

    if (repo_dir / ".git").is_dir():
        info("Repositorio local ya existe en {}".format(repo_dir))
    else:
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        info("Clonando {} ...".format(repo_url))
        res = subprocess.run(["git", "clone", repo_url, str(repo_dir)], capture_output=True, text=True,
                             env=child_env(), encoding="utf-8", errors="replace")
        if res.returncode != 0:
            raise SaveSyncError("No se pudo clonar:\n" + res.stderr.strip())

    ss.config["repo_url"] = repo_url
    ss.config["repo_dir"] = str(repo_dir)
    if args.device:
        ss.config["device_name"] = args.device
    ss.config.setdefault("device_name", ss.device)
    if args.platform:
        ss.config["platform"] = args.platform
    ss.save_config()
    ss.device = ss.config["device_name"]
    ss.platform = ss.config.get("platform") or detect_platform()

    git = ss.git
    # identidad mínima para poder hacer commits
    if git.run("config", "user.email", check=False).stdout.strip() == "":
        git.run("config", "user.email", "savesync@{}".format(ss.device))
    if git.run("config", "user.name", check=False).stdout.strip() == "":
        git.run("config", "user.name", "SaveSync ({})".format(ss.device))

    if not git.has_commits():
        git.run("checkout", "-B", "main", check=False)
    git.pull()

    if not ss.games_path().exists():
        ss.save_games(DEFAULT_GAMES)
        (repo_dir / ".gitattributes").write_text("saves/** binary\n", encoding="utf-8")
        (repo_dir / "README.md").write_text(
            "# Partidas guardadas\n\nRepositorio gestionado por SaveSync. No edites `saves/` a mano.\n",
            encoding="utf-8")
        git.commit_and_push("SaveSync: inicializar repositorio")
        info("Repositorio inicializado con el catálogo de juegos por defecto.")

    info("\nListo. Dispositivo: {}  |  Plataforma: {}".format(ss.device, ss.platform))
    info("Datos locales: {}".format(ss.home))
    info("Siguiente paso: savesync list")


def cmd_list(ss, args):
    ss.git.pull()
    games = ss.load_games()
    info("Dispositivo: {}  |  Plataforma: {}\n".format(ss.device, ss.platform))
    for g in games:
        a = ss.analyze(g)
        m = a["manifest"] or {}
        info("[{}] {}".format(g["id"], g["name"]))
        info("   carpeta local : {}".format(a["path"] or "(sin ruta para esta plataforma; usa set-path)"))
        if a["path"] is not None:
            exists = "sí" if a["path"].is_dir() else "no existe"
            info("   existe        : {}  ({} archivos)".format(exists, len(a["local_files"])))
        if m:
            info("   en GitHub     : subido por '{}' el {}".format(m.get("device"), m.get("updated_at")))
        info("   estado        : {}".format(describe(a["status"])))
        info("")


def describe(status):
    return {
        "sin-ruta": "sin ruta configurada en este dispositivo",
        "vacio": "no hay partidas ni aquí ni en GitHub",
        "sincronizado": "✔ sincronizado",
        "solo-local": "↑ solo existe aquí (se subirá)",
        "solo-remoto": "↓ solo existe en GitHub (se descargará)",
        "remoto-nuevo": "↓ hay una versión más nueva en GitHub",
        "local-nuevo": "↑ la versión local es más nueva",
        "conflicto": "⚠ conflicto: cambió aquí y en otro dispositivo",
    }.get(status, status)


def selected_games(ss, ids):
    games = ss.load_games()
    if not ids:
        return games
    return [ss.get_game(games, i) for i in ids]


def cmd_status(ss, args):
    ss.git.pull()
    for g in selected_games(ss, args.games):
        a = ss.analyze(g)
        info("{:<20} {}".format(g["id"], describe(a["status"])))


def cmd_sync(ss, args, mode="auto"):
    """mode: auto | push | pull"""
    ensure_git()
    ss.git.pull()
    changed = []
    for g in selected_games(ss, args.games):
        a = ss.analyze(g)
        st = a["status"]
        if st in ("sin-ruta", "vacio"):
            if args.games:
                info("- {}: {}".format(g["id"], describe(st)))
            continue

        action = None
        if mode == "push":
            action = "up" if a["local_files"] else None
        elif mode == "pull":
            action = "down" if a["remote_hash"] else None
        elif st == "sincronizado":
            ss.remember(g, a["local_hash"])
        elif st in ("solo-local", "local-nuevo"):
            action = "up"
        elif st in ("solo-remoto", "remoto-nuevo"):
            action = "down"
        elif st == "conflicto":
            if not args.prefer:
                info("- {}".format(g["name"]))
            choice = args.prefer or ss.ask_conflict(g, a)
            action = {"local": "up", "remote": "down"}.get(choice)
            if action is None:
                info("   saltado (resuélvelo con: savesync push {0}  o  savesync pull {0})".format(g["id"]))

        if action is None:
            if st == "sincronizado":
                info("- {}: ✔ al día".format(g["name"]))
            continue
        if action == "up" and a["local_hash"] == a["remote_hash"]:
            info("- {}: ✔ al día".format(g["name"]))
            ss.remember(g, a["local_hash"])
            continue
        if action == "down" and a["local_hash"] == a["remote_hash"]:
            info("- {}: ✔ al día".format(g["name"]))
            ss.remember(g, a["remote_hash"])
            continue

        if args.dry_run:
            info("- {}: (simulación) {}".format(g["name"], "subiría ↑" if action == "up" else "descargaría ↓"))
            continue
        if action == "up":
            info("- {}: subiendo ↑ ({} archivos)".format(g["name"], len(a["local_files"])))
            ss.upload(g, a)
            changed.append(g["name"])
        else:
            m = a["manifest"] or {}
            info("- {}: descargando ↓ (versión de '{}', {})".format(g["name"], m.get("device"), m.get("updated_at")))
            ss.download(g, a)

    if not args.dry_run:
        report_changed = ss.write_report()
        if changed or report_changed:
            what = ", ".join(changed) if changed else "inventario de partidas"
            msg = "{}: {} [{}]".format(ss.device, what, now_iso())
            if ss.git.commit_and_push(msg) and changed:
                info("\nSubido a GitHub ✔")


def cmd_run(ss, args):
    """Sincroniza, ejecuta el juego y vuelve a sincronizar al cerrarlo.
    Ideal como opción de lanzamiento de Steam:  savesync run elden-ring -- %command%"""
    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise SaveSyncError("Falta el comando del juego. Ejemplo: savesync run elden-ring -- %command%")
    ns = argparse.Namespace(games=[args.id], dry_run=False, prefer=None)
    info("== SaveSync: antes de jugar ==")
    try:
        cmd_sync(ss, ns, "auto")
    except SaveSyncError as e:
        warn("No se pudo sincronizar antes de jugar ({}). Se juega igual.".format(e))
    code = subprocess.call(cmd, env=child_env())
    info("== SaveSync: después de jugar ==")
    ss.state = load_json(ss.state_path, {})
    cmd_sync(ss, ns, "auto")
    return code


def cmd_add(ss, args):
    ss.git.pull()
    games = ss.load_games()
    if any(g["id"] == args.id for g in games):
        raise SaveSyncError("Ya existe un juego con id '{}'. Usa set-path para cambiar la ruta local.".format(args.id))
    plat = args.platform or ss.platform
    game = {
        "id": args.id,
        "name": args.name or args.id,
        "paths": {plat: args.path},
        "include": args.include or ["*"],
    }
    if args.exclude:
        game["exclude"] = args.exclude
    games.append(game)
    ss.save_games(games)
    ss.git.commit_and_push("{}: añadir juego {}".format(ss.device, args.id))
    info("Añadido '{}' con ruta para {}: {}".format(game["name"], plat, args.path))
    info("En otros dispositivos indica su carpeta con: savesync set-path {} <ruta>".format(args.id))


def cmd_set_path(ss, args):
    games = ss.load_games()
    game = ss.get_game(games, args.id)
    if args.shared:
        # guarda la ruta en games.json para todos los dispositivos de esta plataforma
        ss.git.pull()
        games = ss.load_games()
        game = ss.get_game(games, args.id)
        game.setdefault("paths", {})[ss.platform] = args.path
        ss.save_games(games)
        ss.git.commit_and_push("{}: ruta {} para {}".format(ss.device, ss.platform, args.id))
        info("Ruta compartida para {} guardada en el repo.".format(ss.platform))
    else:
        ss.config.setdefault("path_overrides", {})[args.id] = args.path
        ss.save_config()
        info("Ruta local para '{}' en este dispositivo: {}".format(game["name"], args.path))


def cmd_backups(ss, args):
    games = selected_games(ss, args.games)
    for g in games:
        d = ss.backup_root / g["id"]
        snaps = sorted(p.name for p in d.iterdir() if p.is_dir()) if d.is_dir() else []
        info("[{}] {}".format(g["id"], ", ".join(snaps) if snaps else "(sin backups)"))
    info("\nCarpeta de backups: {}".format(ss.backup_root))


def cmd_history(ss, args):
    ss.git.pull()
    game = ss.get_game(ss.load_games(), args.id)
    res = ss.git.run("log", "--date=format:%Y-%m-%d %H:%M", "--pretty=format:%h  %ad  %s",
                     "-n", str(args.n), "--", "saves/{}".format(game["id"]))
    info(res.stdout or "(sin historial)")
    info("\nPara recuperar una versión antigua: savesync restore {} <commit>".format(game["id"]))


def cmd_restore(ss, args):
    """Recupera una versión antigua desde el historial de git."""
    ss.git.pull()
    game = ss.get_game(ss.load_games(), args.id)
    a = ss.analyze(game)
    if a["path"] is None:
        raise SaveSyncError("No hay ruta local para este juego.")
    rel = "saves/{}/data".format(game["id"])
    listing = ss.git.run("ls-tree", "-r", "--name-only", args.commit, "--", rel).stdout.split()
    if not listing:
        raise SaveSyncError("Ese commit no contiene partidas de '{}'.".format(game["id"]))
    bk = ss.backup_local(game, a["local_files"])
    if bk:
        info("Backup local en: {}".format(bk))
    for p in a["local_files"].values():
        p.unlink()
    for item in listing:
        blob = subprocess.run(["git", "-C", str(ss.repo_dir), "show", "{}:{}".format(args.commit, item)],
                              capture_output=True, check=True, env=child_env()).stdout
        dst = a["path"] / item[len(rel) + 1:]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(blob)
    remove_empty_dirs(a["path"])
    info("Restaurada la versión {} de {}.".format(args.commit, game["name"]))
    info("Ejecuta 'savesync push {}' si quieres que esta sea la versión en GitHub.".format(game["id"]))


def cmd_scan(ss, args):
    import savesync_detect as det
    ss.git.pull()
    cands = det.scan(ss, progress=info)
    if not cands:
        info("No se encontraron partidas.")
        return
    for c in cands:
        mark = {"new": "NUEVO", "merge": "AÑADIR RUTA", "known": "ya agregado"}[c["status"]]
        info("[{:<11}] {} ({})  {} archivos  {}".format(mark, c["name"], c["system"], c["files"], c["path"]))
    if args.add:
        n = 0
        for c in cands:
            if c["status"] != "known":
                ss.add_or_merge(det.candidate_entry(c), c["path_store"])
                n += 1
        ss.write_report()
        ss.git.commit_and_push("{}: añadir {} juegos detectados".format(ss.device, n))
        info("\nAñadidos/actualizados {} juegos. Ejecuta 'sync' para subir sus partidas.".format(n))


def cmd_remove(ss, args):
    ss.git.pull()
    g = ss.remove_game(args.id, args.delete_remote)
    ss.write_report()
    ss.git.commit_and_push("{}: quitar juego {}".format(ss.device, g["id"]))
    info("Quitado '{}'.{}".format(g["name"], " También se borraron sus partidas del repo (siguen en el historial)."
                                  if args.delete_remote else ""))


def cmd_gui(ss, args):
    import savesync_gui
    savesync_gui.serve(port=args.port, open_browser=not args.no_browser)


def latest_release(timeout=4):
    """Devuelve (version, url) de la última release publicada, o None."""
    if not RELEASES_REPO:
        return None
    import urllib.request
    req = urllib.request.Request("https://api.github.com/repos/{}/releases/latest".format(RELEASES_REPO),
                                 headers={"User-Agent": "SaveSync/" + VERSION,
                                          "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data.get("tag_name", "").lstrip("v"), data.get("html_url")


def _vtuple(v):
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v)[:3])


def update_available():
    try:
        rel = latest_release()
    except Exception:
        return None
    if rel and rel[0] and _vtuple(rel[0]) > _vtuple(VERSION):
        return {"version": rel[0], "url": rel[1]}
    return None


def cmd_info(ss, args):
    info("SaveSync {}".format(VERSION))
    info("Plataforma : {}".format(ss.platform))
    info("Dispositivo: {}".format(ss.device))
    info("Datos      : {}".format(ss.home))
    info("Repo URL   : {}".format(ss.config.get("repo_url", "(sin configurar)")))
    info("Repo local : {}".format(ss.config.get("repo_dir", "(sin configurar)")))
    info("Python     : {} ({})".format(platform.python_version(), sys.executable))
    up = update_available()
    if up:
        info("\nHay una versión nueva: {}  ->  {}".format(up["version"], up["url"]))


# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog="savesync", description="Sincroniza tus partidas guardadas con GitHub.")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("setup", help="configura el repositorio de GitHub en este dispositivo")
    s.add_argument("--repo", help="URL del repo (https o ssh)")
    s.add_argument("--device", help="nombre de este dispositivo (ej: pc, steamdeck, telefono)")
    s.add_argument("--dir", help="dónde clonar el repo localmente")
    s.add_argument("--platform", choices=list(PLATFORM_FALLBACKS), help="forzar plataforma")

    sub.add_parser("list", help="lista los juegos y su estado")

    s = sub.add_parser("status", help="estado resumido")
    s.add_argument("games", nargs="*")

    for name, hlp in (("sync", "sincroniza automáticamente (sube o baja lo más nuevo)"),
                      ("push", "fuerza subir la versión local a GitHub"),
                      ("pull", "fuerza descargar la versión de GitHub")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("games", nargs="*", help="ids de juegos (por defecto: todos)")
        s.add_argument("--dry-run", action="store_true", help="solo muestra lo que haría")
        if name == "sync":
            s.add_argument("--prefer", choices=["local", "remote"], help="cómo resolver conflictos sin preguntar")
        else:
            s.set_defaults(prefer=None)

    s = sub.add_parser("add", help="añade un juego nuevo")
    s.add_argument("id", help="identificador corto, ej: hollow-knight")
    s.add_argument("path", help="carpeta de partidas en este dispositivo")
    s.add_argument("--name", help="nombre visible")
    s.add_argument("--include", nargs="+", help="patrones de archivos a incluir (por defecto todo)")
    s.add_argument("--exclude", nargs="+", help="patrones a excluir")
    s.add_argument("--platform", choices=list(PLATFORM_FALLBACKS), help="plataforma de la ruta")

    s = sub.add_parser("set-path", help="cambia la carpeta de un juego en este dispositivo")
    s.add_argument("id")
    s.add_argument("path")
    s.add_argument("--shared", action="store_true",
                   help="guardarla en el repo para todos los dispositivos de esta plataforma")

    s = sub.add_parser("backups", help="lista los backups locales automáticos")
    s.add_argument("games", nargs="*")

    s = sub.add_parser("history", help="historial de versiones de un juego en GitHub")
    s.add_argument("id")
    s.add_argument("-n", type=int, default=15)

    s = sub.add_parser("restore", help="recupera una versión antigua desde el historial")
    s.add_argument("id")
    s.add_argument("commit")

    s = sub.add_parser("run", help="sincroniza, abre el juego y sincroniza al cerrar")
    s.add_argument("id")
    s.add_argument("command", nargs=argparse.REMAINDER, help="-- comando del juego (en Steam: %%command%%)")

    s = sub.add_parser("scan", help="busca partidas guardadas en este dispositivo")
    s.add_argument("--add", action="store_true", help="añade al catálogo todo lo encontrado")

    s = sub.add_parser("remove", help="quita un juego del catálogo")
    s.add_argument("id")
    s.add_argument("--delete-remote", action="store_true", help="borra también sus partidas del repo")

    s = sub.add_parser("gui", help="abre la interfaz gráfica en el navegador")
    s.add_argument("--port", type=int, default=0, help="puerto (por defecto uno libre)")
    s.add_argument("--no-browser", action="store_true", help="no abrir el navegador automáticamente")

    sub.add_parser("info", help="muestra la configuración")
    return p


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        # sin argumentos (ej. doble clic) abre la interfaz gráfica
        args = parser.parse_args(["gui"])
    ss = SaveSync()
    try:
        handler = {
            "setup": cmd_setup, "list": cmd_list, "status": cmd_status,
            "sync": lambda s, a: cmd_sync(s, a, "auto"),
            "push": lambda s, a: cmd_sync(s, a, "push"),
            "pull": lambda s, a: cmd_sync(s, a, "pull"),
            "add": cmd_add, "set-path": cmd_set_path, "backups": cmd_backups,
            "history": cmd_history, "run": cmd_run,
            "scan": cmd_scan, "remove": cmd_remove, "gui": cmd_gui, "restore": cmd_restore, "info": cmd_info,
        }[args.cmd]
        rc = handler(ss, args)
        if args.cmd == "run" and isinstance(rc, int):
            return rc
    except SaveSyncError as e:
        print("Error: {}".format(e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelado.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
