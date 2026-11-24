"""
SaveSync - autodetección de partidas guardadas.

Fuentes:
  1. Emuladores conocidos (RetroArch, PCSX2, DuckStation, PPSSPP, Dolphin, Ryujinx, yuzu y derivados,
     Citra/Azahar, Cemu, RPCS3) y la carpeta de EmuDeck.
  2. Base de datos de Ludusavi (https://github.com/mtkennerly/ludusavi-manifest), con las rutas de
     guardado de miles de juegos de PC. Se descarga y se guarda en caché la primera vez.
     En Linux / Steam Deck también busca dentro de los prefijos de Proton.
"""
import fnmatch
import json
import os
import re
import time
import urllib.request
from pathlib import Path

import savesync as core

MANIFEST_URL = "https://raw.githubusercontent.com/mtkennerly/ludusavi-manifest/master/data/manifest.yaml"
DB_FILE = "ludusavi-db.json"
DB_MAX_AGE = 14 * 24 * 3600
SD = "/storage/emulated/0"

# ---------------------------------------------------------------------------
# Emuladores: rutas típicas por plataforma (se prueban todas, se usan las que existan)
# ---------------------------------------------------------------------------
EMULATORS = [
    {"id": "retroarch-saves", "name": "RetroArch (partidas .srm)", "system": "Emulador", "item_mode": "files",
     "paths": {
         "windows": ["C:/RetroArch-Win64/saves", "C:/RetroArch/saves", "%APPDATA%/RetroArch/saves",
                     "<steam>/steamapps/common/RetroArch/saves"],
         "linux": ["~/.config/retroarch/saves", "~/.var/app/org.libretro.RetroArch/config/retroarch/saves",
                   "~/snap/retroarch/current/.config/retroarch/saves", "<steam>/steamapps/common/RetroArch/saves"],
         "steamdeck": ["~/Emulation/saves/retroarch/saves"],
         "android": [SD + "/RetroArch/saves", SD + "/Android/data/com.retroarch/files/saves",
                     SD + "/Android/data/com.retroarch.aarch64/files/saves"]}},
    {"id": "retroarch-states", "name": "RetroArch (estados guardados)", "system": "Emulador", "item_mode": "files",
     "paths": {
         "windows": ["C:/RetroArch-Win64/states", "C:/RetroArch/states", "%APPDATA%/RetroArch/states",
                     "<steam>/steamapps/common/RetroArch/states"],
         "linux": ["~/.config/retroarch/states", "~/.var/app/org.libretro.RetroArch/config/retroarch/states",
                   "~/snap/retroarch/current/.config/retroarch/states", "<steam>/steamapps/common/RetroArch/states"],
         "steamdeck": ["~/Emulation/saves/retroarch/states"],
         "android": [SD + "/RetroArch/states", SD + "/Android/data/com.retroarch/files/states",
                     SD + "/Android/data/com.retroarch.aarch64/files/states"]}},
    {"id": "pcsx2", "name": "PCSX2 (memory cards)", "system": "PS2", "item_mode": "files",
     "paths": {
         "windows": ["<documents>/PCSX2/memcards"],
         "linux": ["~/.config/PCSX2/memcards", "~/.var/app/net.pcsx2.PCSX2/config/PCSX2/memcards"],
         "steamdeck": ["~/Emulation/saves/pcsx2/saves"],
         "android": [SD + "/NetherSX2/memcards", SD + "/AetherSX2/memcards"]}},
    {"id": "duckstation", "name": "DuckStation (memory cards)", "system": "PS1", "item_mode": "files",
     "paths": {
         "windows": ["<documents>/DuckStation/memcards", "%LOCALAPPDATA%/DuckStation/memcards"],
         "linux": ["~/.local/share/duckstation/memcards",
                   "~/.var/app/org.duckstation.DuckStation/config/duckstation/memcards"],
         "steamdeck": ["~/Emulation/saves/duckstation/saves"],
         "android": [SD + "/duckstation/memcards"]}},
    {"id": "ppsspp", "name": "PPSSPP", "system": "PSP", "item_mode": "dirs",
     "paths": {
         "windows": ["<documents>/PPSSPP/PSP/SAVEDATA", "C:/PPSSPP/memstick/PSP/SAVEDATA"],
         "linux": ["~/.config/ppsspp/PSP/SAVEDATA", "~/.var/app/org.ppsspp.PPSSPP/config/ppsspp/PSP/SAVEDATA"],
         "steamdeck": ["~/Emulation/saves/ppsspp/saves"],
         "android": [SD + "/PSP/SAVEDATA"]}},
    {"id": "dolphin-gc", "name": "Dolphin (GameCube)", "system": "GameCube", "item_mode": "files",
     "paths": {
         "windows": ["<documents>/Dolphin Emulator/GC", "%APPDATA%/Dolphin Emulator/GC"],
         "linux": ["~/.local/share/dolphin-emu/GC", "~/.var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/GC"],
         "steamdeck": ["~/Emulation/saves/dolphin/GC"],
         "android": [SD + "/dolphin-emu/GC", SD + "/Android/data/org.dolphinemu.dolphinemu/files/GC"]}},
    {"id": "dolphin-wii", "name": "Dolphin (Wii)", "system": "Wii", "item_mode": "dirs",
     "paths": {
         "windows": ["<documents>/Dolphin Emulator/Wii/title", "%APPDATA%/Dolphin Emulator/Wii/title"],
         "linux": ["~/.local/share/dolphin-emu/Wii/title",
                   "~/.var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/Wii/title"],
         "steamdeck": ["~/Emulation/saves/dolphin/Wii"],
         "android": [SD + "/dolphin-emu/Wii/title"]}},
    {"id": "ryujinx", "name": "Ryujinx", "system": "Switch", "item_mode": "dirs",
     "paths": {
         "windows": ["%APPDATA%/Ryujinx/bis/user/save"],
         "linux": ["~/.config/Ryujinx/bis/user/save", "~/.var/app/org.ryujinx.Ryujinx/config/Ryujinx/bis/user/save"],
         "steamdeck": ["~/Emulation/saves/ryujinx/saves"]}},
    {"id": "yuzu", "name": "yuzu / Eden / Citron / Sudachi", "system": "Switch", "item_mode": "dirs",
     "paths": {
         "windows": ["%APPDATA%/yuzu/nand/user/save", "%APPDATA%/eden/nand/user/save",
                     "%APPDATA%/citron/nand/user/save", "%APPDATA%/sudachi/nand/user/save"],
         "linux": ["~/.local/share/yuzu/nand/user/save", "~/.local/share/eden/nand/user/save",
                   "~/.local/share/citron/nand/user/save", "~/.local/share/sudachi/nand/user/save"],
         "steamdeck": ["~/Emulation/saves/yuzu/saves", "~/Emulation/saves/citron/saves",
                       "~/Emulation/saves/eden/saves"]}},
    {"id": "citra", "name": "Citra / Azahar", "system": "3DS", "item_mode": "dirs",
     "paths": {
         "windows": ["%APPDATA%/Azahar/sdmc", "%APPDATA%/Citra/sdmc"],
         "linux": ["~/.local/share/azahar-emu/sdmc", "~/.local/share/citra-emu/sdmc"],
         "steamdeck": ["~/Emulation/saves/citra/saves", "~/Emulation/saves/azahar/saves"],
         "android": [SD + "/citra-emu/sdmc", SD + "/azahar/sdmc"]}},
    {"id": "cemu", "name": "Cemu", "system": "Wii U", "item_mode": "dirs",
     "paths": {
         "windows": ["%APPDATA%/Cemu/mlc01/usr/save", "C:/Cemu/mlc01/usr/save"],
         "linux": ["~/.local/share/Cemu/mlc01/usr/save"],
         "steamdeck": ["~/Emulation/saves/Cemu/saves"]}},
    {"id": "rpcs3", "name": "RPCS3", "system": "PS3", "item_mode": "dirs",
     "paths": {
         "windows": ["C:/RPCS3/dev_hdd0/home/00000001/savedata", "%APPDATA%/rpcs3/dev_hdd0/home/00000001/savedata"],
         "linux": ["~/.config/rpcs3/dev_hdd0/home/00000001/savedata"],
         "steamdeck": ["~/Emulation/saves/rpcs3/saves"]}},
]


def documents_dir():
    """Carpeta Documentos real en Windows (puede estar en OneDrive)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD),
                            ("d", ctypes.c_ubyte * 8)]
            fid = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
            ptr = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(ptr)) == 0:
                path = ptr.value
                ctypes.windll.ole32.CoTaskMemFree(ptr)
                return Path(path)
        except Exception:
            pass
    return Path.home() / "Documents"


# ---------------------------------------------------------------------------
# Base de datos Ludusavi
# ---------------------------------------------------------------------------
def _split_key(text):
    """Separa 'clave: valor' de YAML, soportando claves entre comillas."""
    if text.startswith('"'):
        i, out = 1, []
        while i < len(text):
            ch = text[i]
            if ch == "\\" and i + 1 < len(text):
                nxt = text[i + 1]
                out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt))
                i += 2
                continue
            if ch == '"':
                break
            out.append(ch)
            i += 1
        rest = text[i + 1:]
        key = "".join(out)
    elif text.startswith("'"):
        i, out = 1, []
        while i < len(text):
            if text[i] == "'":
                if text[i + 1:i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                break
            out.append(text[i])
            i += 1
        rest = text[i + 1:]
        key = "".join(out)
    else:
        idx = text.find(": ")
        if idx == -1:
            return (text[:-1], "") if text.endswith(":") else (text, None)
        return text[:idx], text[idx + 2:].strip()
    rest = rest.strip()
    if rest.startswith(":"):
        rest = rest[1:].strip()
    return key, rest


def parse_manifest(lines):
    """Lector mínimo del manifest.yaml de Ludusavi (formato generado, indentación fija)."""
    db = {}
    game = name = section = sub = None
    cur_file = None
    cond = None

    def finish_file():
        if cur_file is None or game is None:
            return
        path, tags, conds = cur_file
        if tags and "save" not in tags:
            return
        oses = None
        if conds:
            if all(c.get("os") for c in conds):
                oses = sorted({c["os"] for c in conds})
        game["f"].append([path, oses])

    for raw in lines:
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if not stripped or stripped == "---" or stripped.startswith("#"):
            continue
        ind = len(line) - len(line.lstrip(" "))
        if ind == 0:
            finish_file()
            cur_file = None
            if game and game["f"]:
                db[name] = [game["s"], game["d"], game["f"]]
            name, _ = _split_key(stripped)
            game = {"s": None, "d": [], "f": []}
            section = sub = None
            continue
        if game is None:
            continue
        if ind == 2:
            finish_file()
            cur_file = None
            section, _ = _split_key(stripped)
            continue
        if section == "files":
            if ind == 4:
                finish_file()
                path, _ = _split_key(stripped)
                cur_file = [path, [], []]
                sub = None
            elif ind == 6 and cur_file is not None:
                sub, _ = _split_key(stripped)
            elif ind >= 8 and cur_file is not None:
                if sub == "tags" and stripped.startswith("- "):
                    cur_file[1].append(stripped[2:].strip())
                elif sub == "when":
                    body = stripped
                    if body.startswith("- "):
                        cond = {}
                        cur_file[2].append(cond)
                        body = body[2:].strip()
                    k, v = _split_key(body)
                    if cond is not None and v:
                        cond[k] = v
        elif section == "steam" and ind == 4:
            k, v = _split_key(stripped)
            if k == "id" and v and v.isdigit():
                game["s"] = int(v)
        elif section == "installDir" and ind == 4:
            k, _ = _split_key(stripped)
            game["d"].append(k)
    finish_file()
    if game and game["f"]:
        db[name] = [game["s"], game["d"], game["f"]]
    return db


def db_path(ss):
    return ss.home / DB_FILE


def db_info(ss):
    p = db_path(ss)
    if not p.exists():
        return {"exists": False}
    st = p.stat()
    try:
        n = json.loads(p.read_text(encoding="utf-8")).get("count", 0)
    except Exception:
        n = 0
    return {"exists": True, "games": n, "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "stale": time.time() - st.st_mtime > DB_MAX_AGE}


def update_db(ss, progress=print):
    progress("Descargando base de datos de Ludusavi (~18 MB)...")
    req = urllib.request.Request(MANIFEST_URL, headers={"User-Agent": "SaveSync/" + core.VERSION})
    with urllib.request.urlopen(req, timeout=120) as r:
        text = r.read().decode("utf-8", errors="replace")
    progress("Procesando...")
    db = parse_manifest(text.splitlines())
    core.save_json(db_path(ss), {"source": MANIFEST_URL, "count": len(db), "games": db})
    progress("Base de datos lista: {} juegos con rutas de guardado.".format(len(db)))
    return len(db)


def load_db(ss):
    data = core.load_json(db_path(ss), None)
    return data.get("games", {}) if data else {}


# ---------------------------------------------------------------------------
# Búsqueda de rutas con caché (case-insensitive)
# ---------------------------------------------------------------------------
class FS:
    def __init__(self):
        self.cache = {}

    def listing(self, path):
        key = str(path)
        if key not in self.cache:
            try:
                with os.scandir(path) as it:
                    self.cache[key] = {e.name.lower(): (e.name, e.is_dir()) for e in it}
            except OSError:
                self.cache[key] = {}
        return self.cache[key]

    def match(self, root, pattern, limit=50):
        comps = [c for c in pattern.replace("\\", "/").split("/") if c and c != "."]
        out = []
        self._match(Path(root), comps, 0, out, limit, 0)
        return out

    def _match(self, cur, comps, i, out, limit, depth):
        if len(out) >= limit:
            return
        if i == len(comps):
            out.append(cur)
            return
        c = comps[i]
        lst = self.listing(cur)
        if not lst:
            return
        if c == "**":
            self._match(cur, comps, i + 1, out, limit, depth)
            if depth < 4:
                for real, is_dir in list(lst.values()):
                    if is_dir:
                        self._match(cur / real, comps, i, out, limit, depth + 1)
        elif any(ch in c for ch in "*?["):
            pat = c.lower()
            for low, (real, _d) in lst.items():
                if fnmatch.fnmatchcase(low, pat):
                    self._match(cur / real, comps, i + 1, out, limit, depth)
        else:
            hit = lst.get(c.lower())
            if hit:
                self._match(cur / hit[0], comps, i + 1, out, limit, depth)


def _contexts(ss, libs):
    """Contextos de búsqueda: (nombre, sistema_operativo_del_juego, raíces, steam_id_filtro)."""
    plat = ss.platform
    home = Path.home()
    ctxs = []
    if plat == "windows":
        up = Path(os.environ.get("USERPROFILE", str(home)))
        roots = {
            "<home>": [up],
            "<winAppData>": [Path(os.environ.get("APPDATA", str(up / "AppData/Roaming")))],
            "<winLocalAppData>": [Path(os.environ.get("LOCALAPPDATA", str(up / "AppData/Local")))],
            "<winLocalAppDataLow>": [up / "AppData/LocalLow"],
            "<winDocuments>": list(dict.fromkeys([documents_dir(), up / "Documents"])),
            "<winPublic>": [Path(os.environ.get("PUBLIC", "C:/Users/Public"))],
            "<winProgramData>": [Path(os.environ.get("PROGRAMDATA", "C:/ProgramData"))],
            "<winDir>": [Path(os.environ.get("WINDIR", "C:/Windows"))],
            "<osUserName>": [os.environ.get("USERNAME", "")],
        }
        ctxs.append({"os": "windows", "roots": roots, "libs": libs, "prefix": None, "appid": None})
    elif plat in ("linux", "steamdeck"):
        roots = {
            "<home>": [home],
            "<xdgData>": [Path(os.environ.get("XDG_DATA_HOME", str(home / ".local/share")))],
            "<xdgConfig>": [Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))],
            "<osUserName>": [os.environ.get("USER", "")],
        }
        ctxs.append({"os": "linux", "roots": roots, "libs": libs, "prefix": None, "appid": None})
        # prefijos de Proton
        for lib in libs:
            cd = lib / "steamapps" / "compatdata"
            if not cd.is_dir():
                continue
            for entry in os.scandir(cd):
                if not entry.name.isdigit():
                    continue
                user = Path(entry.path) / "pfx/drive_c/users/steamuser"
                if not user.is_dir():
                    continue
                roots = {
                    "<home>": [user],
                    "<winAppData>": [user / "AppData/Roaming"],
                    "<winLocalAppData>": [user / "AppData/Local"],
                    "<winLocalAppDataLow>": [user / "AppData/LocalLow"],
                    "<winDocuments>": [user / "Documents", user / "My Documents"],
                    "<winPublic>": [user.parent / "Public"],
                    "<winProgramData>": [user.parent.parent / "ProgramData"],
                    "<osUserName>": ["steamuser"],
                }
                ctxs.append({"os": "windows", "roots": roots, "libs": libs, "prefix": entry.path,
                             "appid": int(entry.name)})
    return ctxs


_PH = re.compile(r"<[A-Za-z]+>")


def _expand_template(tpl, ctx, steam_id, install_dirs):
    """Devuelve [(raiz_concreta, patron_relativo)] para una plantilla de Ludusavi."""
    t = tpl.replace("\\", "/")
    t = t.replace("<storeUserId>", "*")
    t = t.replace("<storeGameId>", str(steam_id or "*"))
    variants = [t]
    if "<game>" in t:
        variants = [t.replace("<game>", d) for d in install_dirs] or []
    results = []
    for v in variants:
        for name, vals in ctx["roots"].items():
            if name == "<osUserName>":
                v = v.replace(name, str(vals[0]))
        m = _PH.match(v)
        if not m:
            continue
        ph = m.group(0)
        rest = v[len(ph):].lstrip("/")
        if _PH.search(rest):
            continue
        if ph in ("<base>", "<root>"):
            for lib in ctx["libs"]:
                if ph == "<root>":
                    results.append((lib, rest))
                else:
                    for d in install_dirs:
                        results.append((lib / "steamapps" / "common" / d, rest))
        elif ph in ctx["roots"]:
            for r in ctx["roots"][ph]:
                results.append((Path(r), rest))
    return results


def _static_prefix(pattern):
    comps = [c for c in pattern.split("/") if c]
    out = []
    for c in comps:
        if any(ch in c for ch in "*?["):
            break
        out.append(c)
    return out, comps[len(out):]


def _summarize(files):
    total = 0
    mt = 0
    for p in files.values():
        try:
            st = p.stat()
        except OSError:
            continue
        total += st.st_size
        mt = max(mt, st.st_mtime)
    return total, mt


def _candidate_from_matches(fs, name, steam_id, groups):
    """groups: lista de (raiz, patron, coincidencias). Calcula carpeta base + patrones include."""
    entries = []
    for root, pattern, matches in groups:
        static, dyn = _static_prefix(pattern)
        m0 = matches[0]
        if static:
            real = fs.match(root, "/".join(static), limit=1)
            base = real[0] if real else Path(root).joinpath(*static)
        else:
            base = Path(root)
        if not dyn:
            # el patrón apunta exactamente a algo concreto
            if m0.is_dir():
                inc = "*"
            else:
                base, inc = m0.parent, m0.name
        else:
            inc = "/".join(dyn).replace("**/", "*").replace("/**", "/*").replace("**", "*")
            if any(m.is_dir() for m in matches):
                inc = inc + "/*"
        entries.append((base, inc))
    # agrupar por carpeta base
    by_base = {}
    for base, inc in entries:
        by_base.setdefault(str(base), set()).add(inc)
    if len(by_base) > 1:
        try:
            common = Path(os.path.commonpath(list(by_base)))
        except ValueError:
            common = None
        roots_depth = min(len(Path(b).parts) for b in by_base) - 1
        if common is not None and len(common.parts) >= roots_depth and len(common.parts) > 3:
            merged = set()
            for b, incs in by_base.items():
                rel = Path(b).relative_to(common).as_posix()
                for inc in incs:
                    merged.add(inc if rel == "." else (rel + "/" + inc if inc != "*" else rel + "/*"))
            by_base = {str(common): merged}
        else:
            # rutas muy separadas: quedarse con la que tiene más archivos
            best = max(by_base, key=lambda b: len(core.collect_files(b, sorted(by_base[b]))))
            by_base = {best: by_base[best]}
    base, incs = next(iter(by_base.items()))
    include = sorted(incs)
    if "*" in include:
        include = ["*"]
    return base, include


def _store_path(ss, path):
    """Convierte una ruta absoluta en una portable (~, %APPDATA%...)."""
    p = str(path).replace("\\", "/")
    subs = []
    if ss.platform == "windows":
        for var in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
            v = os.environ.get(var)
            if v:
                subs.append((v.replace("\\", "/"), "%{}%".format(var)))
    else:
        subs.append((str(Path.home()), "~"))
    for real, rep in subs:
        if p.lower().startswith(real.lower() + "/") or p.lower() == real.lower():
            return rep + p[len(real):]
    return p


def scan_emulators(ss, fs, libs):
    plat = ss.platform
    plats = core.PLATFORM_FALLBACKS.get(plat, [plat])
    found = []
    docs = documents_dir()
    for emu in EMULATORS:
        seen = set()
        for pl in plats:
            for raw in emu["paths"].get(pl, []):
                cands = []
                if "<steam>" in raw:
                    cands = [Path(raw.replace("<steam>", str(lib))) for lib in libs]
                elif "<documents>" in raw:
                    cands = [Path(raw.replace("<documents>", str(docs)))]
                else:
                    cands = [core.expand_path(raw)]
                for c in cands:
                    if c and c.is_dir() and str(c) not in seen:
                        seen.add(str(c))
                        found.append({"name": emu["name"], "system": emu["system"], "type": "collection",
                                      "item_mode": emu["item_mode"], "id": emu["id"], "path": c,
                                      "include": ["*"], "source": "emulador"})
    # EmuDeck: cualquier otra carpeta dentro de ~/Emulation/saves
    if plat in ("steamdeck", "linux"):
        emudeck = Path.home() / "Emulation" / "saves"
        if emudeck.is_dir():
            known = {str(f["path"]) for f in found}
            for d in sorted(emudeck.iterdir()):
                if not d.is_dir():
                    continue
                subs = [s for s in d.iterdir() if s.is_dir()] or [d]
                for s in subs:
                    if str(s) in known:
                        continue
                    found.append({"name": "EmuDeck: {}{}".format(d.name, "" if s == d else " / " + s.name),
                                  "system": "Emulador", "type": "collection", "item_mode": "files",
                                  "id": core.slugify("emudeck-{}-{}".format(d.name, s.name)), "path": s,
                                  "include": ["*"], "source": "emudeck"})
    return found


def scan_pc_games(ss, fs, libs, progress=print):
    db = load_db(ss)
    if not db:
        return []
    ctxs = _contexts(ss, libs)
    by_appid = {}
    for name, (sid, dirs, files) in db.items():
        if sid:
            by_appid.setdefault(sid, []).append(name)
    found = []
    for ctx in ctxs:
        if ctx["appid"]:
            names = by_appid.get(ctx["appid"], [])
        else:
            names = db.keys()
        for name in names:
            sid, dirs, files = db[name]
            groups = []
            for tpl, oses in files:
                if oses and ctx["os"] not in oses:
                    continue
                if ctx["os"] == "linux" and not oses and ("<win" in tpl or "<regH" in tpl):
                    continue
                for root, pattern in _expand_template(tpl, ctx, sid, dirs):
                    if not pattern:
                        continue
                    matches = fs.match(root, pattern)
                    if matches:
                        groups.append((root, pattern, matches))
            if not groups:
                continue
            base, include = _candidate_from_matches(fs, name, sid, groups)
            found.append({"name": name, "system": "PC", "type": "game", "id": core.slugify(name),
                          "steam_id": sid, "path": base, "include": include,
                          "source": "proton" if ctx["prefix"] else "ludusavi"})
    return found


def scan(ss, progress=print, update=True):
    """Busca partidas en este dispositivo. Devuelve una lista de candidatos."""
    fs = FS()
    libs = core.steam_libraries(ss.platform) if ss.platform in ("windows", "linux", "steamdeck") else []
    results = []
    progress("Buscando emuladores...")
    results += scan_emulators(ss, fs, libs)
    if ss.platform in ("windows", "linux", "steamdeck"):
        info_db = db_info(ss)
        if update and (not info_db.get("exists") or info_db.get("stale")):
            try:
                update_db(ss, progress)
            except Exception as e:
                progress("No se pudo descargar la base de datos de juegos de PC: {}".format(e))
        progress("Buscando juegos de PC...")
        results += scan_pc_games(ss, fs, libs, progress)

    games = ss.load_games()
    out, seen = [], set()
    for c in results:
        files = core.collect_files(c["path"], c["include"])
        if not files:
            continue
        key = (str(c["path"]).lower(), tuple(c["include"]))
        if key in seen:
            continue
        seen.add(key)
        size, mt = _summarize(files)
        c["files"] = len(files)
        c["bytes"] = size
        c["modified"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(mt)) if mt else ""
        c["path_store"] = _store_path(ss, c["path"])
        c["path"] = str(c["path"])
        if c.get("type") == "collection":
            c["items"] = [i["name"] for i in core.collection_items(c["path"], c, files)][:50]
        existing = ss.find_existing(games, c)
        if existing is None:
            # mismo nombre que uno ya agregado
            existing = next((g for g in games if g.get("name", "").lower() == c["name"].lower()), None)
        if existing is None:
            c["status"] = "new"
        else:
            c["existing_id"] = existing["id"]
            cur = ss.local_path(existing)
            same = cur is not None and str(cur).lower().rstrip("/\\") == c["path"].lower().rstrip("/\\")
            c["status"] = "known" if same else "merge"
        out.append(c)
    out.sort(key=lambda c: ({"new": 0, "merge": 1, "known": 2}[c["status"]], c["system"] != "PC", c["name"].lower()))
    progress("Encontrados {} juegos/carpetas con partidas.".format(len(out)))
    return out


def candidate_entry(c):
    """Convierte un candidato de scan() en una entrada de games.json."""
    e = {"id": c.get("existing_id") or c["id"], "name": c["name"], "system": c["system"],
         "include": c["include"]}
    if c.get("type") == "collection":
        e["type"] = "collection"
        e["item_mode"] = c.get("item_mode", "files")
    if c.get("steam_id"):
        e["steam_id"] = c["steam_id"]
    return e
