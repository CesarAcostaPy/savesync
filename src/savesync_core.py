"""
SaveSync - núcleo.

Sincroniza partidas guardadas con un repositorio de GitHub usando la API de GitHub
(no necesita git instalado). Funciona en Windows, Linux / Steam Deck y Android.

Estructura del repositorio de partidas:
    games.json                     catálogo de juegos y sus carpetas por plataforma
    saves/<id>/data/...            archivos de partida
    saves/<id>/manifest.json       quién y cuándo subió la última versión
    devices/<dispositivo>.json     inventario de partidas de cada dispositivo
"""
import base64
import datetime as _dt
import fnmatch
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "2.0.0"
RELEASES_REPO = ""      # "usuario/savesync" - lo rellena el workflow al publicar
OAUTH_CLIENT_ID = ""    # Client ID de la OAuth App (inicio de sesión con código) - lo rellena el workflow
API_URL = os.environ.get("SAVESYNC_API", "https://api.github.com").rstrip("/")
WEB_URL = os.environ.get("SAVESYNC_WEB", "https://github.com").rstrip("/")
MAX_FILE_BYTES = 95 * 1024 * 1024

PLATFORM_FALLBACKS = {
    "windows": ["windows"],
    "steamdeck": ["steamdeck", "linux"],
    "linux": ["linux"],
    "android": ["android"],
    "macos": ["macos"],
}
PLATFORM_LABELS = {"windows": "Windows", "steamdeck": "Steam Deck", "linux": "Linux",
                   "android": "Android", "macos": "macOS"}

DEFAULT_GAMES = [
    {
        "id": "elden-ring", "name": "Elden Ring", "system": "PC", "steam_id": 1245620,
        "paths": {
            "windows": "%APPDATA%/EldenRing",
            "steamdeck": "~/.local/share/Steam/steamapps/compatdata/1245620/pfx/drive_c/users/steamuser/AppData/Roaming/EldenRing",
            "linux": "~/.local/share/Steam/steamapps/compatdata/1245620/pfx/drive_c/users/steamuser/AppData/Roaming/EldenRing",
        },
        "include": ["*/ER0000.*"],
        "notes": "La carpeta interna lleva tu SteamID; usa la misma cuenta de Steam en todos los dispositivos.",
    },
    {
        "id": "retroarch-saves", "name": "RetroArch (partidas .srm)", "system": "Emulador", "type": "collection",
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
        "id": "retroarch-states", "name": "RetroArch (estados guardados)", "system": "Emulador", "type": "collection",
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

STATUS_TEXT = {
    "sin-ruta": "sin carpeta en este dispositivo",
    "vacio": "no hay partidas ni aquí ni en GitHub",
    "sincronizado": "✔ sincronizado",
    "solo-local": "↑ solo existe aquí (se subirá)",
    "solo-remoto": "↓ solo existe en GitHub (se descargará)",
    "remoto-nuevo": "↓ hay una versión más nueva en GitHub",
    "local-nuevo": "↑ la versión local es más nueva",
    "conflicto": "⚠ conflicto: cambió aquí y en otro dispositivo",
}


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------
class SaveSyncError(Exception):
    pass


class NetworkError(SaveSyncError):
    pass


class AuthError(SaveSyncError):
    pass


class GitHubError(SaveSyncError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def now_iso():
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def stamp():
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def is_android():
    return (sys.platform == "android" or hasattr(sys, "getandroidapilevel")
            or "ANDROID_ROOT" in os.environ or "com.termux" in os.environ.get("PREFIX", ""))


def detect_platform():
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if is_android():
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
    if is_android() and os.environ.get("FLET_APP_STORAGE_DATA"):
        return Path(os.environ["FLET_APP_STORAGE_DATA"]) / "savesync"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "savesync"


def expand_path(raw):
    if not raw:
        return None
    s = re.sub(r"%([^%]+)%", lambda m: os.environ.get(m.group(1), m.group(0)), str(raw))
    s = os.path.expandvars(os.path.expanduser(s))
    return Path(s)


def portable_path(path, plat=None):
    """Convierte una ruta absoluta en una portable (~, %APPDATA%...)."""
    plat = plat or detect_platform()
    p = str(path).replace("\\", "/")
    subs = []
    if plat == "windows":
        for var in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
            v = os.environ.get(var)
            if v:
                subs.append((v.replace("\\", "/"), "%{}%".format(var)))
    elif plat != "android":
        subs.append((str(Path.home()), "~"))
    for real, rep in subs:
        if p.lower().startswith(real.lower() + "/") or p.lower() == real.lower():
            return rep + p[len(real):]
    return p


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def save_json(path, data, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if private:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


def dump_json(data):
    return (json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n").encode("utf-8")


def blob_sha(data):
    """SHA-1 de un blob de git (el mismo que usa GitHub)."""
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def file_blob_sha(path):
    size = os.path.getsize(path)
    h = hashlib.sha1()
    h.update(b"blob %d\0" % size)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def matches(rel, include, exclude):
    ok = any(fnmatch.fnmatch(rel, p) for p in include)
    return ok and not any(fnmatch.fnmatch(rel, p) for p in exclude)


def collect_files(root, include=None, exclude=None):
    """{ruta_relativa_posix: Path} de los archivos que coinciden."""
    include = include or ["*"]
    exclude = exclude or []
    out = {}
    if root is None:
        return out
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


def fingerprint_shas(shas):
    """Hash conjunto a partir de {rel: blob_sha}. None si está vacío."""
    if not shas:
        return None
    h = hashlib.sha256()
    for rel in sorted(shas):
        h.update(rel.encode("utf-8") + b"\0" + shas[rel].encode() + b"\n")
    return h.hexdigest()


def local_shas(files):
    return {rel: file_blob_sha(p) for rel, p in files.items()}


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return s[:60] or "juego"


def copy_into(files_bytes, dest_root):
    dest_root = Path(dest_root)
    for rel, data in files_bytes.items():
        dst = dest_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        with open(dst, "wb") as f:
            f.write(data)


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


def human_bytes(b):
    if b is None:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024 or unit == "GB":
            return ("{:.0f} {}" if unit == "B" else "{:.1f} {}").format(b, unit)
        b /= 1024.0


# ---------------------------------------------------------------------------
# Colecciones (carpetas de emuladores con muchas partidas dentro)
# ---------------------------------------------------------------------------
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


def collection_items(rels, game):
    """Lista los juegos individuales dentro de una colección, a partir de rutas relativas."""
    mode = game.get("item_mode", "files")
    default_sys = game.get("system", "")
    items = {}
    for rel in rels:
        parts = rel.split("/")
        system = default_sys
        if len(parts) > 1 and parts[0].lower() in CORE_SYSTEMS:
            system = CORE_SYSTEMS[parts[0].lower()]
            parts = parts[1:]
        name = parts[0] if (mode == "dirs" and len(parts) > 1) else clean_item_name(parts[-1])
        if name.lower() in ("desktop.ini", ".ds_store", "thumbs.db"):
            continue
        items[(name, system)] = items.get((name, system), 0) + 1
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
        try:
            text = (root / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="ignore")
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


# ---------------------------------------------------------------------------
# HTTP / GitHub
# ---------------------------------------------------------------------------
_SSL = None


def ssl_context():
    global _SSL
    if _SSL is None:
        try:
            import certifi  # type: ignore
            _SSL = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _SSL = ssl.create_default_context()
    return _SSL


def http_request(method, url, token=None, body=None, form=None, accept="application/vnd.github+json", timeout=60):
    headers = {"User-Agent": "SaveSync/" + VERSION, "Accept": accept}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = "Bearer " + token
    if url.startswith(API_URL):
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        ctx = ssl_context() if url.startswith("https") else None
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError) as e:
        raise NetworkError("No hay conexión con GitHub ({}).".format(getattr(e, "reason", e)))


class GitHub:
    def __init__(self, token):
        self.token = token

    def call(self, method, path, body=None, ok=(200, 201), raw=False):
        url = path if path.startswith("http") else API_URL + path
        accept = "application/vnd.github.raw+json" if raw else "application/vnd.github+json"
        status, content, headers = http_request(method, url, self.token, body=body, accept=accept)
        if status in ok:
            if raw:
                return content
            return json.loads(content.decode("utf-8")) if content else {}
        try:
            msg = json.loads(content.decode("utf-8")).get("message", "")
        except Exception:
            msg = content[:200].decode("utf-8", "replace")
        if status == 401:
            raise AuthError("La sesión de GitHub no es válida o expiró. Vuelve a conectar tu cuenta.")
        if status == 403 and headers.get("X-RateLimit-Remaining", headers.get("x-ratelimit-remaining")) == "0":
            raise GitHubError(status, "Se alcanzó el límite de uso de la API de GitHub. Prueba en unos minutos.")
        if status == 403 and method != "GET" and "/repos/" in path:
            raise GitHubError(status, "El token no tiene permiso para escribir en el repositorio. En GitHub, "
                                      "dale el permiso «Contents: Read and write». ({})".format(msg))
        if status in (403, 404) and "/repos/" in path and not path.endswith("/git/blobs"):
            raise GitHubError(status, msg or "Sin acceso al repositorio.")
        raise GitHubError(status, "GitHub respondió {}: {}".format(status, msg))

    def user(self):
        return self.call("GET", "/user")

    def repos(self):
        out, page = [], 1
        while page <= 5:
            batch = self.call("GET", "/user/repos?per_page=100&sort=updated&page={}".format(page))
            out += batch
            if len(batch) < 100:
                break
            page += 1
        return out

    def create_repo(self, name, private=True):
        return self.call("POST", "/user/repos", {"name": name, "private": private, "auto_init": True,
                                                  "description": "Partidas guardadas (SaveSync)"})


def device_flow_start(client_id=None, scope="repo"):
    client_id = client_id or OAUTH_CLIENT_ID
    if not client_id:
        raise SaveSyncError("Esta versión no tiene configurado el inicio de sesión con código. Usa un token.")
    status, content, _ = http_request("POST", WEB_URL + "/login/device/code",
                                      form={"client_id": client_id, "scope": scope}, accept="application/json")
    data = json.loads(content.decode("utf-8") or "{}")
    if status != 200 or "device_code" not in data:
        raise SaveSyncError("GitHub no aceptó el inicio de sesión: {}".format(
            data.get("error_description") or data.get("error") or status))
    return data


def device_flow_poll(device_code, client_id=None):
    """Devuelve ("ok", token) o (estado, None): pending, slow_down, expired, denied."""
    client_id = client_id or OAUTH_CLIENT_ID
    status, content, _ = http_request("POST", WEB_URL + "/login/oauth/access_token", accept="application/json",
                                      form={"client_id": client_id, "device_code": device_code,
                                            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    data = json.loads(content.decode("utf-8") or "{}")
    if data.get("access_token"):
        return "ok", data["access_token"]
    err = data.get("error", "")
    return {"authorization_pending": "pending", "slow_down": "slow_down", "expired_token": "expired",
            "access_denied": "denied"}.get(err, err or "pending"), None


# ---------------------------------------------------------------------------
# Repositorio remoto (vía API de GitHub, con caché local)
# ---------------------------------------------------------------------------
class RemoteStore:
    def __init__(self, gh, repo, cache_dir):
        self.gh = gh
        self.repo = repo
        self.cache_dir = Path(cache_dir)
        self.branch = None
        self.private = None
        self.snap = None        # {"head", "tree", "files": {path: [sha, size]}}
        self.offline = False
        self._tree_cache = {}

    # --- básico ----------------------------------------------------------
    def _p(self, suffix):
        return "/repos/{}{}".format(self.repo, suffix)

    def _cache_file(self):
        return self.cache_dir / "snapshot-{}.json".format(slugify(self.repo))

    def ensure_ready(self):
        if self.branch:
            return
        info = self.gh.call("GET", self._p(""))
        self.branch = info.get("default_branch") or "main"
        self.private = info.get("private")
        perms = info.get("permissions") or {}
        if perms and not perms.get("push", True):
            raise SaveSyncError("Tu cuenta no tiene permiso de escritura en {}.".format(self.repo))

    def _head(self):
        try:
            ref = self.gh.call("GET", self._p("/git/ref/heads/" + urllib.parse.quote(self.branch)))
            return ref["object"]["sha"]
        except GitHubError as e:
            if e.status in (404, 409):  # repositorio vacío
                self.gh.call("PUT", self._p("/contents/README.md"), {
                    "message": "SaveSync: inicializar repositorio",
                    "content": base64.b64encode(
                        "# Partidas guardadas\n\nRepositorio gestionado por SaveSync.\n".encode("utf-8")).decode(),
                    "branch": self.branch})
                ref = self.gh.call("GET", self._p("/git/ref/heads/" + urllib.parse.quote(self.branch)))
                return ref["object"]["sha"]
            raise

    def _tree(self, tree_sha):
        if tree_sha in self._tree_cache:
            return self._tree_cache[tree_sha]
        t = self.gh.call("GET", self._p("/git/trees/{}?recursive=1".format(tree_sha)))
        files = {e["path"]: [e["sha"], e.get("size", 0)] for e in t.get("tree", []) if e.get("type") == "blob"}
        self._tree_cache[tree_sha] = files
        return files

    def snapshot(self, refresh=True):
        if self.snap is not None and not refresh:
            return self.snap
        try:
            self.ensure_ready()
            head = self._head()
            if self.snap and self.snap["head"] == head:
                self.offline = False
                return self.snap
            commit = self.gh.call("GET", self._p("/git/commits/" + head))
            files = self._tree(commit["tree"]["sha"])
            self.snap = {"head": head, "tree": commit["tree"]["sha"], "files": files, "branch": self.branch}
            self.offline = False
            save_json(self._cache_file(), self.snap)
        except NetworkError:
            cached = load_json(self._cache_file(), None)
            if cached is None:
                raise
            self.snap, self.offline = cached, True
            self.branch = self.branch or cached.get("branch", "main")
        return self.snap

    def files(self, prefix):
        snap = self.snapshot(refresh=False)
        n = len(prefix)
        return {p[n:]: v[0] for p, v in snap["files"].items() if p.startswith(prefix)}

    def read_sha(self, sha):
        cache = self.cache_dir / "blobs" / sha[:2] / sha
        try:
            return cache.read_bytes()
        except OSError:
            pass
        data = self.gh.call("GET", self._p("/git/blobs/" + sha), raw=True)
        if isinstance(data, dict):  # por si devuelve JSON
            data = base64.b64decode(data.get("content", ""))
        if blob_sha(data) != sha:
            raise SaveSyncError("El archivo descargado de GitHub está dañado; vuelve a intentar.")
        if len(data) < 2 * 1024 * 1024:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(data)
        return data

    def read(self, path):
        entry = self.snapshot(refresh=False)["files"].get(path)
        return self.read_sha(entry[0]) if entry else None

    def read_json(self, path, default):
        data = self.read(path)
        if data is None:
            return default
        try:
            return json.loads(data.decode("utf-8"))
        except ValueError:
            return default

    # --- escritura -------------------------------------------------------
    def commit(self, build_changes, message):
        """build_changes(store) -> {ruta: bytes | None (borrar)}. Reintenta si otro dispositivo
        subió algo a la vez. Devuelve el sha del commit o None si no había cambios."""
        if self.offline:
            raise NetworkError("Sin conexión con GitHub: no se puede subir ahora.")
        uploaded = set()
        for _attempt in range(6):
            snap = self.snapshot(refresh=True)
            if self.offline:
                raise NetworkError("Sin conexión con GitHub: no se puede subir ahora.")
            changes = build_changes(self)
            entries = []
            for path, data in changes.items():
                cur = snap["files"].get(path)
                if data is None:
                    if cur:
                        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                    continue
                if len(data) > MAX_FILE_BYTES:
                    raise SaveSyncError("{} pesa más de 95 MB y GitHub no lo acepta.".format(path))
                sha = blob_sha(data)
                if cur and cur[0] == sha:
                    continue
                if sha not in uploaded:
                    res = self.gh.call("POST", self._p("/git/blobs"),
                                       {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"})
                    if res.get("sha") != sha:
                        raise SaveSyncError("GitHub guardó un archivo distinto al enviado.")
                    uploaded.add(sha)
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": sha})
            if not entries:
                return None
            tree = self.gh.call("POST", self._p("/git/trees"), {"base_tree": snap["tree"], "tree": entries})
            commit = self.gh.call("POST", self._p("/git/commits"),
                                  {"message": message, "tree": tree["sha"], "parents": [snap["head"]]})
            try:
                self.gh.call("PATCH", self._p("/git/refs/heads/" + urllib.parse.quote(self.branch)),
                             {"sha": commit["sha"], "force": False})
            except GitHubError as e:
                if e.status in (409, 422):
                    time.sleep(0.5)
                    continue  # otro dispositivo subió algo: rehacer sobre lo nuevo
                raise
            self.snapshot(refresh=True)
            return commit["sha"]
        raise SaveSyncError("GitHub cambió varias veces mientras se subía. Intenta de nuevo.")

    # --- historial -------------------------------------------------------
    def history(self, path, n=30):
        self.ensure_ready()
        res = self.gh.call("GET", self._p("/commits?sha={}&path={}&per_page={}".format(
            urllib.parse.quote(self.branch), urllib.parse.quote(path), n)))
        out = []
        for c in res:
            cm = c.get("commit", {})
            out.append({"sha": c["sha"], "short": c["sha"][:7], "message": cm.get("message", ""),
                        "date": (cm.get("committer") or cm.get("author") or {}).get("date", "")})
        return out

    def files_at(self, commit_sha, prefix):
        commit = self.gh.call("GET", self._p("/git/commits/" + commit_sha))
        files = self._tree(commit["tree"]["sha"])
        n = len(prefix)
        return {p[n:]: v[0] for p, v in files.items() if p.startswith(prefix)}


# ---------------------------------------------------------------------------
# SaveSync
# ---------------------------------------------------------------------------
class SaveSync:
    def __init__(self, log=print):
        self.log = log
        self.home = data_dir()
        self.config_path = self.home / "config.json"
        self.state_path = self.home / "state.json"
        self.backup_root = self.home / "backups"
        self.lock = threading.RLock()
        self.reload()

    def reload(self):
        self.config = load_json(self.config_path, {})
        self.state = load_json(self.state_path, {})
        self.platform = self.config.get("platform") or detect_platform()
        self.device = self.config.get("device_name") or socket.gethostname() or "dispositivo"
        self._store = None

    # --- configuración -----------------------------------------------------
    @property
    def logged_in(self):
        return bool(self.config.get("token"))

    @property
    def configured(self):
        return bool(self.config.get("token") and self.config.get("repo"))

    def save_config(self):
        save_json(self.config_path, self.config, private=True)

    def save_state(self):
        save_json(self.state_path, self.state)

    @property
    def gh(self):
        if not self.logged_in:
            raise AuthError("Conecta tu cuenta de GitHub primero.")
        return GitHub(self.config["token"])

    @property
    def store(self):
        if not self.configured:
            raise SaveSyncError("Elige primero el repositorio de partidas.")
        if self._store is None:
            self._store = RemoteStore(self.gh, self.config["repo"], self.home / "cache")
        return self._store

    def login_with_token(self, token, kind="token"):
        token = token.strip()
        user = GitHub(token).user()
        self.config.update(token=token, token_kind=kind, login=user.get("login"))
        self.save_config()
        self._store = None
        return user

    def logout(self):
        for k in ("token", "token_kind", "login"):
            self.config.pop(k, None)
        self.save_config()
        self._store = None

    def set_repo(self, full_name, device_name=None):
        store = RemoteStore(self.gh, full_name, self.home / "cache")
        try:
            store.snapshot(refresh=True)
        except GitHubError as e:
            if e.status in (403, 404):
                raise SaveSyncError(
                    "No se puede acceder a {} con este token. Si es un token «fine-grained», edítalo en GitHub "
                    "y en «Repository access» agrega ese repositorio, con el permiso «Contents: Read and write». "
                    "(Detalle de GitHub: {})".format(full_name, e))
            raise
        self.config["repo"] = full_name
        if device_name:
            self.config["device_name"] = device_name.strip()
            self.device = self.config["device_name"]
        self.config.setdefault("device_name", self.device)
        self.save_config()
        self._store = store
        if "games.json" not in store.snap["files"]:
            store.commit(lambda st: {"games.json": dump_json({"version": 1, "games": DEFAULT_GAMES}),
                                     ".gitattributes": b"saves/** binary\n"},
                         "SaveSync: catálogo inicial de juegos")
            self.log("Repositorio preparado con el catálogo inicial.")
        return store

    def set_device_name(self, new):
        new = (new or "").strip()
        if not new or new == self.device:
            return
        old_report = self.report_path()
        self.config["device_name"] = new
        self.save_config()
        self.device = new
        if self.configured:
            games = self.load_games()
            report = self.build_report(games)
            self.store.commit(lambda st: {old_report: None, self.report_path(): self.report_bytes(report)},
                              "{}: renombrar dispositivo".format(new))

    # --- catálogo ----------------------------------------------------------
    def load_games(self, store=None):
        st = store or self.store
        return st.read_json("games.json", {"games": []}).get("games", [])

    def mutate_games(self, fn, message, extra=None):
        """Aplica fn(games) sobre la versión más reciente de games.json y sube el resultado."""
        result = {}

        def build(st):
            games = self.load_games(st)
            result["value"] = fn(games)
            changes = {"games.json": dump_json({"version": 1, "games": games})}
            if extra:
                changes.update(extra(st, games))
            return changes
        self.store.commit(build, message)
        return result.get("value")

    @staticmethod
    def get_game(games, gid):
        for g in games:
            if g["id"] == gid:
                return g
        raise SaveSyncError("No existe el juego '{}'.".format(gid))

    @staticmethod
    def find_existing(games, entry):
        for g in games:
            if g["id"] == entry.get("id"):
                return g
            if entry.get("steam_id") and g.get("steam_id") == entry.get("steam_id"):
                return g
        name = (entry.get("name") or "").lower()
        return next((g for g in games if name and g.get("name", "").lower() == name), None)

    def local_path(self, game):
        override = self.config.get("path_overrides", {}).get(game["id"])
        if override:
            return expand_path(override)
        for plat in PLATFORM_FALLBACKS.get(self.platform, [self.platform]):
            raw = game.get("paths", {}).get(plat)
            if raw:
                p = expand_path(raw)
                if not p.exists() and game.get("steam_id") and self.platform in ("linux", "steamdeck"):
                    m = re.search(r"steamapps/compatdata/(\d+)/(.*)$", p.as_posix())
                    if m:
                        for lib in steam_libraries(self.platform):
                            cand = lib / "steamapps" / "compatdata" / m.group(1) / m.group(2)
                            if cand.exists():
                                return cand
                return p
        return None

    def add_or_merge(self, entry, path, shared=True):
        """Añade un juego o, si ya existe, le pone la carpeta de esta plataforma."""
        def fn(games):
            g = self.find_existing(games, entry)
            created = g is None
            if created:
                g = {k: v for k, v in entry.items() if v not in (None, "", [])}
                base_id = g.get("id") or slugify(g.get("name", "juego"))
                g["id"], n = base_id, 1
                while any(x["id"] == g["id"] for x in games):
                    n += 1
                    g["id"] = "{}-{}".format(base_id, n)
                g["paths"] = {}
                g.setdefault("include", ["*"])
                games.append(g)
            if shared:
                g.setdefault("paths", {})[self.platform] = path
            return dict(g), created
        g, created = self.mutate_games(fn, "{}: {} {}".format(
            self.device, "añadir" if True else "", entry.get("name", entry.get("id"))))
        if not shared:
            self.config.setdefault("path_overrides", {})[g["id"]] = path
            self.save_config()
        return g, created

    def add_many(self, entries_paths):
        """Añade varios juegos detectados en un solo commit. entries_paths = [(entry, path)]"""
        out = []

        def fn(games):
            out.clear()
            for entry, path in entries_paths:
                g = self.find_existing(games, entry)
                created = g is None
                if created:
                    g = {k: v for k, v in entry.items() if v not in (None, "", [])}
                    base_id = g.get("id") or slugify(g.get("name", "juego"))
                    g["id"], n = base_id, 1
                    while any(x["id"] == g["id"] for x in games):
                        n += 1
                        g["id"] = "{}-{}".format(base_id, n)
                    g["paths"] = {}
                    g.setdefault("include", ["*"])
                    games.append(g)
                g.setdefault("paths", {})[self.platform] = path
                out.append((g["id"], g.get("name"), created))
        self.mutate_games(fn, "{}: añadir {} juegos detectados".format(self.device, len(entries_paths)))
        return list(out)

    def set_path(self, gid, path, shared=True, include=None, exclude=None):
        if shared:
            def fn(games):
                g = self.get_game(games, gid)
                g.setdefault("paths", {})[self.platform] = path
                if include is not None:
                    g["include"] = include or ["*"]
                if exclude is not None:
                    if exclude:
                        g["exclude"] = exclude
                    else:
                        g.pop("exclude", None)
            self.mutate_games(fn, "{}: carpeta {} para {}".format(self.device, self.platform, gid))
            self.config.get("path_overrides", {}).pop(gid, None)
        else:
            self.config.setdefault("path_overrides", {})[gid] = path
        self.save_config()

    def remove_game(self, gid, delete_remote=False):
        def fn(games):
            g = self.get_game(games, gid)
            games[:] = [x for x in games if x["id"] != gid]
            return g

        def extra(st, games):
            if not delete_remote:
                return {}
            prefix = "saves/{}/".format(gid)
            return {p: None for p in st.snapshot(refresh=False)["files"] if p.startswith(prefix)}
        g = self.mutate_games(fn, "{}: quitar {}".format(self.device, gid), extra)
        self.state.pop(gid, None)
        self.save_state()
        self.config.get("path_overrides", {}).pop(gid, None)
        self.save_config()
        return g

    # --- estado de un juego -------------------------------------------------
    def local_files(self, game):
        p = self.local_path(game)
        return p, (collect_files(p, game.get("include"), game.get("exclude")) if p else {})

    def remote_shas(self, game):
        return self.store.files("saves/{}/data/".format(game["id"]))

    def analyze(self, game, with_manifest=True):
        path, lfiles = self.local_files(game)
        lsh = local_shas(lfiles)
        lhash = fingerprint_shas(lsh)
        rsh = self.remote_shas(game)
        rhash = fingerprint_shas(rsh)
        manifest = None
        if with_manifest and rhash:
            manifest = self.store.read_json("saves/{}/manifest.json".format(game["id"]), None)
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
        return {"path": path, "local_files": lfiles, "local_shas": lsh, "local_hash": lhash,
                "remote_shas": rsh, "remote_hash": rhash, "manifest": manifest, "base": base, "status": status}

    def remember(self, gid, h):
        self.state[gid] = {"hash": h, "synced_at": now_iso()}
        self.save_state()

    # --- reportes por dispositivo -----------------------------------------
    def report_path(self):
        return "devices/{}.json".format(slugify(self.device))

    def build_report(self, games):
        out = {}
        for g in games:
            path, files = self.local_files(g)
            if not files:
                continue
            stats = [p.stat() for p in files.values()]
            entry = {"files": len(files), "bytes": sum(s.st_size for s in stats),
                     "modified": _dt.datetime.fromtimestamp(max(s.st_mtime for s in stats))
                     .astimezone().isoformat(timespec="seconds")}
            if g.get("type") == "collection":
                entry["items"] = collection_items(files.keys(), g)
            out[g["id"]] = entry
        return out

    def report_bytes(self, report):
        return dump_json({"device": self.device, "platform": self.platform, "updated_at": now_iso(),
                          "games": report})

    def report_changes(self, st, report):
        old = st.read_json(self.report_path(), {})
        if old.get("games") == report and old.get("platform") == self.platform:
            return {}
        return {self.report_path(): self.report_bytes(report)}

    def load_reports(self):
        out = []
        for path in self.store.snapshot(refresh=False)["files"]:
            if path.startswith("devices/") and path.endswith(".json"):
                r = self.store.read_json(path, None)
                if r:
                    out.append(r)
        return out

    # --- backups locales ----------------------------------------------------
    def backup_local(self, gid, files):
        if not files:
            return None
        base = self.backup_root / gid / stamp()
        dest, n = base, 1
        while dest.exists():
            n += 1
            dest = base.with_name("{}-{}".format(base.name, n))
        for rel, src in files.items():
            d = dest / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, d)
        keep = int(self.config.get("keep_backups", 10))
        snaps = sorted(p for p in (self.backup_root / gid).iterdir() if p.is_dir())
        for old in snaps[:-keep]:
            shutil.rmtree(old, ignore_errors=True)
        return dest

    def list_backups(self, gid):
        d = self.backup_root / gid
        return sorted((p.name for p in d.iterdir() if p.is_dir()), reverse=True) if d.is_dir() else []

    # --- sincronización ---------------------------------------------------
    def _write_local(self, game, a, rfiles_bytes):
        path = a["path"]
        bk = self.backup_local(game["id"], a["local_files"])
        if bk:
            self.log("   copia de seguridad: {}".format(bk))
        path.mkdir(parents=True, exist_ok=True)
        for rel, p in a["local_files"].items():
            if rel not in rfiles_bytes:
                p.unlink()
        copy_into(rfiles_bytes, path)
        remove_empty_dirs(path)

    def sync(self, ids=None, mode="auto", prefer=None, ask=None, dry_run=False):
        """mode: auto | push | pull. ask(game, analysis) -> 'local' | 'remote' | None.
        Devuelve lista de (id, nombre, acción) con acción en up/down/ok/skip/conflict/none."""
        with self.lock:
            st = self.store
            st.snapshot(refresh=True)
            if st.offline:
                self.log("⚠ Sin conexión con GitHub: se muestran los datos guardados.")
            games = self.load_games()
            if ids:
                wanted = [self.get_game(games, i) for i in ids]
            else:
                wanted = games
            results, uploads = [], []
            for g in wanted:
                a = self.analyze(g)
                s = a["status"]
                action = None
                if s in ("sin-ruta", "vacio"):
                    results.append((g["id"], g["name"], "none"))
                    continue
                if mode == "push":
                    action = "up" if a["local_files"] else None
                elif mode == "pull":
                    action = "down" if a["remote_hash"] else None
                elif s == "sincronizado":
                    self.remember(g["id"], a["local_hash"])
                elif s in ("solo-local", "local-nuevo"):
                    action = "up"
                elif s in ("solo-remoto", "remoto-nuevo"):
                    action = "down"
                elif s == "conflicto":
                    choice = prefer or (ask(g, a) if ask else None)
                    action = {"local": "up", "remote": "down"}.get(choice)
                    if action is None:
                        self.log("- {}: ⚠ conflicto (sin resolver)".format(g["name"]))
                        results.append((g["id"], g["name"], "conflict"))
                        continue
                if action and a["local_hash"] == a["remote_hash"]:
                    self.remember(g["id"], a["local_hash"])
                    action = None
                if action is None:
                    self.log("- {}: ✔ al día".format(g["name"]))
                    results.append((g["id"], g["name"], "ok"))
                    continue
                if dry_run:
                    self.log("- {}: (simulación) {}".format(g["name"], "subiría" if action == "up" else "bajaría"))
                    results.append((g["id"], g["name"], action))
                    continue
                if action == "down":
                    m = a["manifest"] or {}
                    self.log("- {}: ↓ descargando (versión de '{}', {})".format(
                        g["name"], m.get("device", "?"), (m.get("updated_at") or "")[:16].replace("T", " ")))
                    if a["path"] is None:
                        raise SaveSyncError("{} no tiene carpeta en este dispositivo.".format(g["name"]))
                    data = {rel: st.read_sha(sha) for rel, sha in a["remote_shas"].items()}
                    self._write_local(g, a, data)
                    self.remember(g["id"], a["remote_hash"])
                    results.append((g["id"], g["name"], "down"))
                else:
                    self.log("- {}: ↑ subiendo ({} archivos)".format(g["name"], len(a["local_files"])))
                    uploads.append((g, a))
                    results.append((g["id"], g["name"], "up"))

            if dry_run:
                return results
            report = self.build_report(games)
            names = [g["name"] for g, _ in uploads]

            def build(st2):
                changes = {}
                for g, a in uploads:
                    prefix = "saves/{}/".format(g["id"])
                    for p in st2.snapshot(refresh=False)["files"]:
                        if p.startswith(prefix + "data/") and p[len(prefix) + 5:] not in a["local_files"]:
                            changes[p] = None
                    for rel, p in a["local_files"].items():
                        changes[prefix + "data/" + rel] = p.read_bytes()
                    changes[prefix + "manifest.json"] = dump_json({
                        "game": g["id"], "hash": a["local_hash"], "device": self.device,
                        "platform": self.platform, "updated_at": now_iso(),
                        "files": {r: {"sha": s, "size": a["local_files"][r].stat().st_size}
                                  for r, s in a["local_shas"].items()}})
                changes.update(self.report_changes(st2, report))
                return changes
            if uploads or not st.offline:
                try:
                    sha = st.commit(build, "{}: {} [{}]".format(
                        self.device, ", ".join(names) if names else "inventario de partidas", now_iso()))
                except NetworkError:
                    if uploads:
                        raise
                    sha = None
                for g, a in uploads:
                    self.remember(g["id"], a["local_hash"])
                if sha and uploads:
                    self.log("Subido a GitHub ✔")
            return results

    # --- historial / restaurar --------------------------------------------
    def history(self, gid, n=30):
        return self.store.history("saves/{}/data".format(gid), n)

    def restore(self, gid, commit_sha):
        games = self.load_games()
        g = self.get_game(games, gid)
        a = self.analyze(g, with_manifest=False)
        if a["path"] is None:
            raise SaveSyncError("Este juego no tiene carpeta en este dispositivo.")
        files = self.store.files_at(commit_sha, "saves/{}/data/".format(gid))
        if not files:
            raise SaveSyncError("Esa versión no contiene partidas de este juego.")
        data = {rel: self.store.read_sha(sha) for rel, sha in files.items()}
        self._write_local(g, a, data)
        self.log("Restaurada la versión {} de {}.".format(commit_sha[:7], g["name"]))

    def restore_backup(self, gid, name):
        games = self.load_games()
        g = self.get_game(games, gid)
        a = self.analyze(g, with_manifest=False)
        src = self.backup_root / gid / name
        files = {p.relative_to(src).as_posix(): p.read_bytes() for p in src.rglob("*") if p.is_file()}
        self._write_local(g, a, files)


# ---------------------------------------------------------------------------
# Actualizaciones
# ---------------------------------------------------------------------------
def _vtuple(v):
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v)[:3])


def update_available():
    if not RELEASES_REPO:
        return None
    try:
        status, content, _ = http_request("GET", "{}/repos/{}/releases/latest".format(API_URL, RELEASES_REPO),
                                          timeout=5)
        if status != 200:
            return None
        data = json.loads(content.decode("utf-8"))
    except Exception:
        return None
    v = (data.get("tag_name") or "").lstrip("v")
    if v and _vtuple(v) > _vtuple(VERSION):
        return {"version": v, "url": data.get("html_url")}
    return None


def child_env():
    """Entorno para procesos hijos (el juego en 'run'). En ejecutables de PyInstaller
    restaura LD_LIBRARY_PATH para no afectar a los juegos de Steam."""
    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
            orig = env.pop(var + "_ORIG", None)
            if orig is not None:
                env[var] = orig
            else:
                env.pop(var, None)
    return env
