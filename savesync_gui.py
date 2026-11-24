"""
SaveSync - interfaz gráfica local (se abre en el navegador).

    python savesync.py gui

Solo escucha en 127.0.0.1 y cada petición lleva un token aleatorio, así que
ninguna otra página web ni otro equipo de la red puede usarla.
"""
import argparse
import contextlib
import io
import json
import os
import secrets
import shutil
import string
import sys
import subprocess
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import savesync as core
import savesync_detect as det

LOCK = threading.Lock()
TOKEN = secrets.token_urlsafe(24)
PLATFORM_LABELS = {"windows": "Windows", "steamdeck": "Steam Deck", "linux": "Linux",
                   "android": "Android", "macos": "macOS"}


# ---------------------------------------------------------------------------
# Lógica
# ---------------------------------------------------------------------------
def new_ss():
    ss = core.SaveSync()
    ss.interactive = False
    return ss


@contextlib.contextmanager
def capture():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield buf


def build_state(ss, pull=False):
    out = {"configured": bool(ss.config.get("repo_dir")), "device": ss.device, "platform": ss.platform,
           "platform_label": PLATFORM_LABELS.get(ss.platform, ss.platform), "version": core.VERSION,
           "platform_labels": PLATFORM_LABELS, "home": str(ss.home), "warning": None}
    if not out["configured"] or not (ss.repo_dir / ".git").is_dir():
        out["configured"] = False
        return out
    out["repo_url"] = ss.config.get("repo_url")
    if pull:
        try:
            ss.git.pull()
        except core.SaveSyncError as e:
            out["warning"] = "No se pudo actualizar desde GitHub (¿sin internet?). Se muestran los datos locales.\n" + str(e)

    games = ss.load_games()
    live = ss.build_report(games)
    reports = [r for r in ss.load_reports() if r.get("device") != ss.device]
    reports.append({"device": ss.device, "platform": ss.platform, "games": live,
                    "updated_at": core.now_iso(), "this": True})
    out["devices"] = [{"device": r.get("device"), "platform": r.get("platform"),
                       "updated_at": r.get("updated_at"), "this": bool(r.get("this")),
                       "games": len(r.get("games", {}))} for r in reports]

    rows, game_list, merged = [], [], {}
    for g in games:
        a = ss.analyze(g)
        m = a["manifest"] or {}
        gh = {"device": m.get("device"), "platform": m.get("platform"), "updated_at": m.get("updated_at"),
              "files": len(m.get("files", {}))} if m else None
        ginfo = {
            "id": g["id"], "name": g.get("name", g["id"]), "system": g.get("system", ""),
            "type": g.get("type", "game"), "paths": g.get("paths", {}), "include": g.get("include", ["*"]),
            "exclude": g.get("exclude", []), "steam_id": g.get("steam_id"), "notes": g.get("notes", ""),
            "local_path": str(a["path"]) if a["path"] else None,
            "override": ss.config.get("path_overrides", {}).get(g["id"]),
            "status": a["status"], "status_text": core.describe(a["status"]), "github": gh,
            "where": [dict(device=r["device"], platform=r["platform"], this=bool(r.get("this")),
                           **{k: v for k, v in r["games"][g["id"]].items() if k != "items"})
                      for r in reports if g["id"] in r.get("games", {})],
        }
        game_list.append(ginfo)
        if g.get("type") == "collection":
            items = {}
            for r in reports:
                rg = r.get("games", {}).get(g["id"])
                if not rg:
                    continue
                for it in rg.get("items", []):
                    k = (it["name"], it.get("system", ""))
                    items.setdefault(k, {"where": [], "github": False})
                    items[k]["where"].append({"device": r["device"], "platform": r["platform"],
                                              "this": bool(r.get("this"))})
            if a["remote_hash"]:
                for it in core.collection_items(ss.remote_dir(g) / "data", g):
                    k = (it["name"], it.get("system", ""))
                    items.setdefault(k, {"where": [], "github": False})
                    items[k]["github"] = True
            for (name, system), v in items.items():
                key = "item::" + name.lower()
                r = merged.get(key)
                if r is None:
                    r = merged[key] = {"key": key, "name": name, "system": "", "game_id": g["id"],
                                       "game_ids": [], "item": True, "parents": [], "where": [], "github": None}
                    rows.append(r)
                # el sistema más específico gana sobre "Emulador"
                if system and (not r["system"] or r["system"] == "Emulador"):
                    r["system"] = system
                if g["id"] not in r["game_ids"]:
                    r["game_ids"].append(g["id"])
                    r["parents"].append(ginfo["name"])
                seen_dev = {(w["device"]) for w in r["where"]}
                r["where"] += [w for w in v["where"] if w["device"] not in seen_dev]
                if v["github"] and (r["github"] is None or (gh or {}).get("updated_at", "") > r["github"].get("updated_at", "")):
                    r["github"] = gh
            if not items:
                rows.append({"key": g["id"], "name": ginfo["name"], "system": ginfo["system"],
                             "game_id": g["id"], "item": False, "where": [], "github": None, "empty": True})
        else:
            rows.append({"key": g["id"], "name": ginfo["name"], "system": ginfo["system"], "game_id": g["id"],
                         "item": False,
                         "where": [{"device": w["device"], "platform": w["platform"], "this": w["this"],
                                    "files": w.get("files"), "modified": w.get("modified")}
                                   for w in ginfo["where"]],
                         "github": gh, "empty": not ginfo["where"] and not gh})
    for r in rows:
        if r.get("item"):
            r["parent"] = ", ".join(r.pop("parents"))
            r["system"] = r["system"] or "Emulador"
    out["games"] = game_list
    out["rows"] = rows
    out["db"] = det.db_info(ss)
    return out


def op_sync(ss, body):
    ns = argparse.Namespace(games=body.get("games") or [], dry_run=False, prefer=body.get("prefer"))
    core.cmd_sync(ss, ns, body.get("mode", "auto"))


def to_store(ss, raw):
    raw = (raw or "").strip().strip('"')
    if not raw:
        raise core.SaveSyncError("Indica una carpeta.")
    if any(t in raw for t in ("%", "~", "$")):
        return raw
    return det._store_path(ss, Path(raw))


def op_add(ss, body):
    ss.git.pull()
    name = (body.get("name") or "").strip()
    if not name:
        raise core.SaveSyncError("El nombre es obligatorio.")
    path = to_store(ss, body.get("path"))
    if not core.expand_path(path).is_dir():
        print("Aviso: la carpeta todavía no existe en este dispositivo.")
    entry = {"id": core.slugify(body.get("id") or name), "name": name, "system": body.get("system") or "PC",
             "include": [p for p in body.get("include", []) if p] or ["*"]}
    if body.get("exclude"):
        entry["exclude"] = [p for p in body["exclude"] if p]
    if body.get("type") == "collection":
        entry["type"] = "collection"
        entry["item_mode"] = body.get("item_mode") or "files"
    games = ss.load_games()
    if any(g["id"] == entry["id"] for g in games):
        raise core.SaveSyncError("Ya existe un juego con id '{}'. Edita su carpeta desde 'Mis partidas'."
                                 .format(entry["id"]))
    g, _ = ss.add_or_merge(entry, path, shared=body.get("shared", True))
    ss.write_report()
    ss.git.commit_and_push("{}: añadir juego {}".format(ss.device, g["id"]))
    print("Añadido '{}' ({}).".format(g["name"], path))
    if body.get("sync"):
        op_sync(ss, {"games": [g["id"]]})
    return {"id": g["id"]}


def op_add_detected(ss, body):
    ss.git.pull()
    ids = []
    for c in body.get("candidates", []):
        g, created = ss.add_or_merge(det.candidate_entry(c), c["path_store"])
        ids.append(g["id"])
        print("{} {}".format("Añadido:" if created else "Ruta añadida a:", g["name"]))
    ss.write_report()
    ss.git.commit_and_push("{}: añadir {} juegos detectados".format(ss.device, len(ids)))
    if body.get("sync") and ids:
        print("")
        op_sync(ss, {"games": ids})
    return {"ids": ids}


def op_set_path(ss, body):
    gid = body["id"]
    path = to_store(ss, body.get("path"))
    ss.git.pull()
    games = ss.load_games()
    g = ss.get_game(games, gid)
    if body.get("shared", True):
        g.setdefault("paths", {})[ss.platform] = path
        ss.config.get("path_overrides", {}).pop(gid, None)
        ss.save_config()
        if "include" in body:
            g["include"] = [p for p in body["include"] if p] or ["*"]
        ss.save_games(games)
        ss.write_report()
        ss.git.commit_and_push("{}: ruta {} para {}".format(ss.device, ss.platform, gid))
    else:
        ss.config.setdefault("path_overrides", {})[gid] = path
        ss.save_config()
    print("Carpeta de '{}' en este dispositivo: {}".format(g["name"], path))


def op_remove(ss, body):
    ss.git.pull()
    g = ss.remove_game(body["id"], body.get("delete_remote", False))
    ss.write_report()
    ss.git.commit_and_push("{}: quitar juego {}".format(ss.device, g["id"]))
    print("Quitado '{}'.".format(g["name"]))


def op_setup(ss, body):
    ns = argparse.Namespace(repo=(body.get("repo") or "").strip(), device=(body.get("device") or "").strip() or None,
                            dir=None, platform=None)
    if not ns.repo:
        raise core.SaveSyncError("Indica la URL del repositorio.")
    core.cmd_setup(ss, ns)


def op_config(ss, body):
    new = (body.get("device_name") or "").strip()
    if new and new != ss.device:
        old_report = ss.report_path()
        ss.config["device_name"] = new
        ss.save_config()
        ss.device = new
        if old_report.exists():
            old_report.unlink()
        ss.write_report()
        ss.git.commit_and_push("{}: renombrar dispositivo".format(new))
        print("Dispositivo renombrado a '{}'.".format(new))
    if "keep_backups" in body:
        ss.config["keep_backups"] = max(1, int(body["keep_backups"]))
        ss.save_config()


def op_history(ss, body):
    g = ss.get_game(ss.load_games(), body["id"])
    res = ss.git.run("log", "--date=format:%Y-%m-%d %H:%M", "--pretty=format:%h\t%ad\t%s", "-n", "30",
                     "--", "saves/{}".format(g["id"]), check=False)
    commits = []
    for line in res.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            commits.append({"commit": parts[0], "date": parts[1], "message": parts[2]})
    return {"commits": commits}


def op_restore(ss, body):
    core.cmd_restore(ss, argparse.Namespace(id=body["id"], commit=body["commit"]))


def op_browse(ss, q):
    raw = (q.get("path") or [""])[0]
    shortcuts = [{"label": "Inicio", "path": str(Path.home())}]
    if ss.platform == "windows":
        for var, label in (("APPDATA", "AppData\\Roaming"), ("LOCALAPPDATA", "AppData\\Local")):
            if os.environ.get(var):
                shortcuts.append({"label": label, "path": os.environ[var]})
        shortcuts.append({"label": "AppData\\LocalLow", "path": str(Path.home() / "AppData" / "LocalLow")})
        shortcuts.append({"label": "Documentos", "path": str(det.documents_dir())})
        shortcuts.append({"label": "Saved Games", "path": str(Path.home() / "Saved Games")})
        for d in string.ascii_uppercase:
            if os.path.exists(d + ":\\"):
                shortcuts.append({"label": d + ":", "path": d + ":\\"})
    elif ss.platform == "android":
        shortcuts.append({"label": "Almacenamiento", "path": det.SD})
    else:
        for p, label in ((Path.home() / "Emulation", "Emulation (EmuDeck)"), (Path.home() / ".config", ".config"),
                         (Path.home() / ".local/share", ".local/share")):
            if p.is_dir():
                shortcuts.append({"label": label, "path": str(p)})
        for lib in core.steam_libraries(ss.platform):
            cd = lib / "steamapps" / "compatdata"
            if cd.is_dir():
                shortcuts.append({"label": "Proton: " + str(lib), "path": str(cd)})
    path = Path(core.expand_path(raw)) if raw else Path.home()
    if not path.is_dir():
        path = Path.home()
    dirs = []
    try:
        for e in sorted(os.scandir(path), key=lambda e: e.name.lower()):
            try:
                if e.is_dir():
                    dirs.append(e.name)
            except OSError:
                pass
    except OSError as e:
        return {"path": str(path), "parent": str(path.parent), "dirs": [], "shortcuts": shortcuts,
                "error": str(e)}
    names = None
    try:
        names = [n for n in os.listdir(path) if os.path.isfile(os.path.join(path, n))][:200]
    except OSError:
        names = []
    return {"path": str(path), "parent": str(path.parent) if path.parent != path else None,
            "dirs": dirs, "files": names, "shortcuts": shortcuts}


def op_preview(ss, body):
    path = core.expand_path(to_store(ss, body.get("path")))
    inc = [p for p in body.get("include", []) if p] or ["*"]
    exc = [p for p in body.get("exclude", []) if p]
    files = core.collect_files(path, inc, exc)
    size = sum(p.stat().st_size for p in files.values())
    res = {"exists": path.is_dir(), "path": str(path), "files": len(files), "bytes": size,
           "sample": sorted(files)[:15]}
    if body.get("type") == "collection":
        res["items"] = [i["name"] for i in core.collection_items(
            path, {"include": inc, "exclude": exc, "item_mode": body.get("item_mode", "files")}, files)][:30]
    return res


POST_OPS = {
    "sync": op_sync, "add": op_add, "add-detected": op_add_detected, "set-path": op_set_path,
    "remove": op_remove, "setup": op_setup, "config": op_config, "history": op_history,
    "restore": op_restore, "preview": op_preview,
    "scan": lambda ss, b: {"candidates": det.scan(ss, progress=print, update=b.get("update", True))},
    "update-db": lambda ss, b: {"count": det.update_db(ss, progress=print)},
}


# ---------------------------------------------------------------------------
# Servidor HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "SaveSync"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost") and self.headers.get("X-Token") == TOKEN

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if u.path in ("/", "/index.html"):
            if parse_qs(u.query).get("t", [""])[0] != TOKEN:
                return self._send(403, "Abre SaveSync desde el enlace que muestra la terminal.".encode("utf-8"),
                                  "text/plain; charset=utf-8")
            return self._send(200, PAGE.replace("__TOKEN__", TOKEN).encode("utf-8"), "text/html; charset=utf-8")
        if not self._authorized():
            return self._send(403, {"ok": False, "error": "No autorizado"})
        q = parse_qs(u.query)
        if u.path == "/api/update":  # consulta de red, fuera del bloqueo
            return self._send(200, {"ok": True, "update": core.update_available()})
        with LOCK:
            ss = new_ss()
            try:
                with capture() as buf:
                    if u.path == "/api/state":
                        data = build_state(ss, pull=q.get("pull", ["0"])[0] == "1")
                    elif u.path == "/api/browse":
                        data = op_browse(ss, q)
                    else:
                        return self._send(404, {"ok": False, "error": "No existe"})
                data["ok"] = True
                data["log"] = buf.getvalue()
                self._send(200, data)
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e), "trace": traceback.format_exc()})

    def do_POST(self):
        if not self._authorized():
            return self._send(403, {"ok": False, "error": "No autorizado"})
        name = urlparse(self.path).path.rsplit("/", 1)[-1]
        op = POST_OPS.get(name)
        if not op:
            return self._send(404, {"ok": False, "error": "No existe"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"ok": False, "error": "JSON inválido"})
        with LOCK:
            ss = new_ss()
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    res = op(ss, body) or {}
                res.update(ok=True, log=buf.getvalue())
                self._send(200, res)
            except core.SaveSyncError as e:
                self._send(200, {"ok": False, "error": str(e), "log": buf.getvalue()})
            except Exception as e:
                self._send(200, {"ok": False, "error": "{}: {}".format(type(e).__name__, e),
                                 "log": buf.getvalue() + "\n" + traceback.format_exc()})


def open_url(url):
    if core.detect_platform() == "android" and shutil.which("termux-open-url"):
        subprocess.Popen(["termux-open-url", url], env=core.child_env())
        return
    if sys.platform.startswith("linux") and shutil.which("xdg-open"):
        try:
            subprocess.Popen(["xdg-open", url], env=core.child_env(),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


def serve(port=0, open_browser=True):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:{}/?t={}".format(httpd.server_address[1], TOKEN)
    print("SaveSync está abierto en tu navegador.")
    print("Si no se abrió, copia esta dirección:\n  " + url)
    print("Cierra esta ventana (o pulsa Ctrl+C) para salir.", flush=True)
    if open_browser:
        threading.Timer(0.6, open_url, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nSaveSync cerrado.")
    finally:
        httpd.server_close()


# ---------------------------------------------------------------------------
# Página
# ---------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SaveSync</title>
<link rel="icon" type="image/png" href="data:image/png;base64,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">
<style>
:root{
  --bg:#0d0f14; --panel:#151922; --panel2:#1b2030; --line:#262c3d; --text:#e7e9ef; --muted:#8b93a7;
  --accent:#7c5cff; --accent2:#5b8cff; --ok:#2fbf71; --warn:#f2b441; --bad:#ef5b5b;
  --windows:#3b82f6; --steamdeck:#b26bff; --linux:#f59e0b; --android:#22c55e; --macos:#9ca3af; --github:#cfd5e2;
  --r:12px;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
button,input,select{font:inherit;color:inherit}
a{color:var(--accent2)}
header{display:flex;align-items:center;gap:14px;padding:14px 22px;border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,#141827,#0d0f14);position:sticky;top:0;z-index:5}
.logo{display:flex;align-items:center;gap:10px;font-weight:700;font-size:18px;letter-spacing:.2px}
.logo i{width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,var(--accent),var(--accent2));
  display:grid;place-items:center;font-style:normal;font-size:15px}
.devchip{padding:4px 10px;border-radius:999px;background:var(--panel2);border:1px solid var(--line);color:var(--muted);font-size:12.5px}
.devchip b{color:var(--text);font-weight:600}
.spacer{flex:1}
.btn{border:1px solid var(--line);background:var(--panel2);padding:8px 14px;border-radius:9px;cursor:pointer;white-space:nowrap}
.btn:hover{border-color:#3a4258;background:#20263a}
.btn.primary{background:linear-gradient(135deg,var(--accent),var(--accent2));border:0;font-weight:600}
.btn.primary:hover{filter:brightness(1.08)}
.btn.small{padding:5px 10px;font-size:12.5px}
.btn.danger{color:#ffb4b4;border-color:#5a2a2a}
.btn:disabled{opacity:.5;cursor:default}
nav{display:flex;gap:4px;padding:10px 22px 0;border-bottom:1px solid var(--line);overflow-x:auto}
nav button{white-space:nowrap;background:none;border:0;padding:10px 14px;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;font-weight:500}
nav button.on{color:var(--text);border-color:var(--accent)}
main{padding:18px 22px 120px;max-width:1300px;margin:0 auto}
.toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}
.seg{display:flex;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:3px;flex-wrap:wrap}
.seg button{border:0;background:none;padding:6px 12px;border-radius:7px;cursor:pointer;color:var(--muted);display:flex;gap:6px;align-items:center}
.seg button.on{background:var(--panel2);color:var(--text)}
.seg .n{font-size:11px;color:var(--muted);background:#0006;border-radius:6px;padding:0 6px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
input[type=text],input[type=search],input[type=number],select{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:7px 10px;outline:none}
input:focus,select:focus{border-color:var(--accent)}
label.chk{display:flex;gap:6px;align-items:center;color:var(--muted);cursor:pointer;user-select:none}
.summary{color:var(--muted);margin:0 0 12px;font-size:13px}
.list{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
.row{display:grid;grid-template-columns:40px minmax(180px,2.2fr) 110px minmax(160px,1.6fr) minmax(130px,1.1fr) 84px;
  gap:12px;align-items:center;padding:10px 14px;border-top:1px solid var(--line);cursor:pointer}
.row:first-child{border-top:0}
.row:hover{background:#1a1f2c}
.row.head{cursor:default;background:#11141c;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.5px}
.row.head:hover{background:#11141c}
.av{width:36px;height:36px;border-radius:9px;display:grid;place-items:center;font-weight:700;font-size:14px;color:#fff}
.name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sub{color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badge{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11.5px;background:var(--panel2);border:1px solid var(--line);white-space:nowrap}
.chips{display:flex;flex-wrap:wrap;gap:5px}
.pchip{display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border-radius:999px;font-size:11.5px;border:1px solid}
.st{font-size:12px;white-space:nowrap}
.st.ok{color:var(--ok)} .st.up,.st.down{color:var(--accent2)} .st.bad{color:var(--warn)} .st.none{color:var(--muted)}
.empty{padding:40px;text-align:center;color:var(--muted)}
.group{margin-bottom:22px}
.group h3{display:flex;align-items:center;gap:8px;margin:0 0 8px;font-size:15px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:18px;margin-bottom:16px}
.card h2{margin:0 0 6px;font-size:17px}
.card p{color:var(--muted);margin:0 0 12px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px 10px;border-top:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.4px;border-top:0}
td.mono,.kv .mono{word-break:break-all}
td .path{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;color:var(--muted);word-break:break-all}
.form{display:grid;grid-template-columns:180px 1fr;gap:12px 16px;align-items:center;max-width:860px}
.form label{color:var(--muted)}
.form .full{grid-column:1/-1}
.inline{display:flex;gap:8px;align-items:center}
.inline input[type=text]{flex:1}
.hint{color:var(--muted);font-size:12px}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.modal-bg{position:fixed;inset:0;background:#000a;display:none;align-items:center;justify-content:center;z-index:20;padding:16px}
.modal-bg.on{display:flex}
.modal{background:var(--panel);border:1px solid var(--line);border-radius:14px;width:min(820px,100%);max-height:88vh;display:flex;flex-direction:column}
.modal header{position:static;background:none;padding:14px 18px}
.modal .body{padding:16px 18px;overflow:auto}
.modal footer{padding:12px 18px;border-top:1px solid var(--line);display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap}
.browser-list{border:1px solid var(--line);border-radius:10px;max-height:46vh;overflow:auto}
.browser-list div{padding:7px 12px;border-top:1px solid var(--line);cursor:pointer}
.browser-list div:first-child{border-top:0}
.browser-list div:hover{background:#1f2537}
.browser-list .f{color:var(--muted);cursor:default}
.browser-list .f:hover{background:none}
.sc{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.log{position:fixed;left:0;right:0;bottom:0;background:#0a0c11;border-top:1px solid var(--line);z-index:10;transition:height .2s}
.log .bar{display:flex;align-items:center;gap:10px;padding:8px 22px;cursor:pointer;color:var(--muted);font-size:12.5px}
.log pre{margin:0;padding:0 22px 14px;height:0;overflow:auto;font:12px/1.5 ui-monospace,Consolas,monospace;color:#c9cfdd;white-space:pre-wrap}
.log.open pre{height:200px}
.busy{position:fixed;inset:0;background:#0007;display:none;place-items:center;z-index:30}
.busy.on{display:grid}
.spinner{width:42px;height:42px;border:4px solid #fff3;border-top-color:var(--accent);border-radius:50%;animation:sp 1s linear infinite;margin:0 auto 12px}
@keyframes sp{to{transform:rotate(360deg)}}
.toast{position:fixed;right:20px;bottom:60px;background:var(--panel2);border:1px solid var(--line);padding:12px 16px;border-radius:10px;
  z-index:40;max-width:420px;box-shadow:0 8px 30px #0008;display:none}
.toast.on{display:block}.toast.err{border-color:var(--bad)}
.warnbox{background:#3a2d12;border:1px solid #6b5220;color:#ffd98a;padding:10px 14px;border-radius:10px;margin-bottom:14px;white-space:pre-wrap}
.kv{display:grid;grid-template-columns:170px 1fr;gap:6px 14px}
.kv div:nth-child(odd){color:var(--muted)}
.items{display:flex;flex-wrap:wrap;gap:6px;max-height:180px;overflow:auto}
@media (max-width:820px){
  .row{grid-template-columns:36px 1fr auto;}
  .row > :nth-child(3),.row > :nth-child(5){display:none}
  .row > :nth-child(4){grid-column:2/4}
  .row.head{display:none}
  .form{grid-template-columns:1fr}
  header{flex-wrap:wrap}
}
</style>
</head>
<body>
<header>
  <div class="logo"><i>⇅</i>SaveSync</div>
  <span class="devchip" id="devchip"></span>
  <div class="spacer"></div>
  <button class="btn" onclick="refresh(true)" title="Traer lo último de GitHub">⟳ Actualizar</button>
  <button class="btn primary" id="syncAll" onclick="syncGames([])">⇅ Sincronizar todo</button>
</header>
<nav id="nav">
  <button data-tab="games" class="on">Mis partidas</button>
  <button data-tab="detect">Detectar</button>
  <button data-tab="add">Agregar manual</button>
  <button data-tab="settings">Ajustes</button>
</nav>
<main id="main"></main>

<div class="modal-bg" id="modal"><div class="modal">
  <header><b id="mTitle"></b><div class="spacer"></div><button class="btn small" onclick="closeModal()">✕</button></header>
  <div class="body" id="mBody"></div>
  <footer id="mFoot"></footer>
</div></div>
<div class="busy" id="busy"><div style="text-align:center"><div class="spinner"></div><div id="busyText">Trabajando…</div></div></div>
<div class="toast" id="toast"></div>
<div class="log" id="log"><div class="bar" onclick="this.parentNode.classList.toggle('open')">▤ Registro <span id="logLast" class="hint"></span></div><pre id="logText"></pre></div>

<script>
const TOKEN = "__TOKEN__";
const PCOL = {windows:"var(--windows)",steamdeck:"var(--steamdeck)",linux:"var(--linux)",android:"var(--android)",macos:"var(--macos)",github:"var(--github)"};
const SYSCOL = ["#7c5cff","#5b8cff","#2fbf71","#f59e0b","#ef5b5b","#14b8a6","#e879f9","#f97316","#84cc16","#06b6d4"];
let S = null, tab = "games", filt = {plat:"all", sys:"", q:"", view:"list", empty:false};
let scanResults = null, scanSel = new Set();
let addForm = {name:"",id:"",system:"PC",type:"game",item_mode:"files",path:"",include:"*",exclude:"",shared:true,sync:true};

const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtB = b => b==null?"":b<1024?b+" B":b<1048576?(b/1024).toFixed(1)+" KB":b<1073741824?(b/1048576).toFixed(1)+" MB":(b/1073741824).toFixed(2)+" GB";
const fmtD = d => d ? String(d).replace("T"," ").slice(0,16) : "";
const plab = p => (S && S.platform_labels && S.platform_labels[p]) || p;
function sysColor(s){let h=0;for(const c of String(s))h=(h*31+c.charCodeAt(0))>>>0;return SYSCOL[h%SYSCOL.length]}
function initials(n){const w=String(n).replace(/[^\p{L}\p{N} ]/gu," ").trim().split(/\s+/);return ((w[0]||"?")[0]+(w[1]?w[1][0]:"")).toUpperCase()}

async function api(path, body, busyText){
  if (busyText) busy(busyText);
  try{
    const opt = {headers:{"X-Token":TOKEN}};
    if (body !== undefined){opt.method="POST";opt.body=JSON.stringify(body);opt.headers["Content-Type"]="application/json"}
    const r = await fetch(path, opt);
    const j = await r.json();
    if (j.log) log(j.log);
    if (!j.ok){ toast(j.error || "Error", true); if (j.trace) log(j.trace); }
    return j;
  }catch(e){ toast("No hay conexión con SaveSync. ¿Cerraste la ventana del programa?", true); return {ok:false} }
  finally{ if (busyText) busy(false) }
}
function busy(t){ $("#busy").classList.toggle("on", !!t); if (t) $("#busyText").textContent = t }
function toast(msg, err){ const t=$("#toast"); t.textContent=msg; t.className="toast on"+(err?" err":""); clearTimeout(t._h); t._h=setTimeout(()=>t.className="toast", err?7000:3500) }
function log(text){ text=String(text).trim(); if(!text) return; const p=$("#logText"); const ts=new Date().toLocaleTimeString();
  p.textContent += `\n[${ts}]\n${text}\n`; p.scrollTop=p.scrollHeight; $("#logLast").textContent="— "+text.split("\n").filter(Boolean).pop().slice(0,120) }

async function refresh(pull){
  const j = await api("/api/state"+(pull?"?pull=1":""), undefined, pull?"Consultando GitHub…":null);
  if (!j.ok) return;
  S = j; render();
  if (pull && !j.warning) toast("Actualizado");
}

document.querySelectorAll("#nav button").forEach(b => b.onclick = () => { tab=b.dataset.tab;
  document.querySelectorAll("#nav button").forEach(x=>x.classList.toggle("on", x===b)); render() });

function render(){
  if (!S) return;
  $("#devchip").innerHTML = `<b>${esc(S.device)}</b> · ${esc(S.platform_label)}`;
  $("#syncAll").disabled = !S.configured;
  const m = $("#main");
  if (!S.configured){ m.innerHTML = setupView(); return }
  let html = S.warning ? `<div class="warnbox">${esc(S.warning)}</div>` : "";
  if (tab==="games") html += gamesView();
  else if (tab==="detect") html += detectView();
  else if (tab==="add") html += addView();
  else html += settingsView();
  m.innerHTML = html;
  if (tab==="add") bindAdd();
}

/* ---------------- Mis partidas ---------------- */
function rowPlatforms(r){ const s=new Set(r.where.map(w=>w.platform)); return s }
function filteredRows(){
  const q = filt.q.toLowerCase();
  return S.rows.filter(r => {
    if (r.empty && !filt.empty) return false;
    if (filt.sys && r.system!==filt.sys) return false;
    if (q && !(r.name.toLowerCase().includes(q) || (r.parent||"").toLowerCase().includes(q))) return false;
    if (filt.plat==="github") return !!r.github;
    if (filt.plat!=="all") return rowPlatforms(r).has(filt.plat);
    return true;
  }).sort((a,b)=>a.name.localeCompare(b.name,"es",{sensitivity:"base"}));
}
function gamesView(){
  const rows = S.rows.filter(r=>!r.empty || filt.empty);
  const plats = {};
  for (const r of rows) for (const p of rowPlatforms(r)) plats[p]=(plats[p]||0)+1;
  if (!(S.platform in plats)) plats[S.platform]=0;
  const ghN = rows.filter(r=>r.github).length;
  const systems = [...new Set(S.rows.map(r=>r.system).filter(Boolean))].sort();
  const segBtn = (k,label,n,col) => `<button class="${filt.plat===k?"on":""}" onclick="setF('plat','${k}')">${col?`<span class="dot" style="background:${col}"></span>`:""}${esc(label)} <span class="n">${n}</span></button>`;
  let h = `<div class="toolbar">
    <div class="seg">${segBtn("all","Todas",rows.length)}${Object.keys(plats).sort().map(p=>segBtn(p,plab(p),plats[p],PCOL[p])).join("")}${segBtn("github","En GitHub",ghN,PCOL.github)}</div>
    <select onchange="setF('sys',this.value)"><option value="">Todos los sistemas</option>${systems.map(s=>`<option ${s===filt.sys?"selected":""}>${esc(s)}</option>`).join("")}</select>
    <input type="search" placeholder="Buscar juego…" value="${esc(filt.q)}" oninput="filt.q=this.value;renderList()" style="min-width:200px">
    <div class="seg"><button class="${filt.view==="list"?"on":""}" onclick="setF('view','list')">Todos juntos</button><button class="${filt.view==="group"?"on":""}" onclick="setF('view','group')">Por plataforma</button></div>
    <label class="chk"><input type="checkbox" ${filt.empty?"checked":""} onchange="setF('empty',this.checked)"> Mostrar sin partidas</label>
  </div><div id="listArea"></div>`;
  setTimeout(renderList);
  return h;
}
function setF(k,v){ filt[k]=v; render() }
function renderList(){
  const el = $("#listArea"); if (!el) return;
  const rows = filteredRows();
  const devs = S.devices.length;
  let h = `<p class="summary">${rows.length} juego${rows.length===1?"":"s"} · ${devs} dispositivo${devs===1?"":"s"} con SaveSync: ${S.devices.map(d=>`<span class="dot" style="background:${PCOL[d.platform]||"#888"}"></span> ${esc(d.device)}${d.this?" (este)":""}`).join(" &nbsp; ")}</p>`;
  if (!rows.length){ el.innerHTML = h + `<div class="list"><div class="empty">No hay partidas con este filtro.<br><br><button class="btn" onclick="goTab('detect')">Detectar partidas en este dispositivo</button></div></div>`; return }
  if (filt.view==="group"){
    const groups = {};
    for (const r of rows){ const ps=[...rowPlatforms(r)]; if(r.github) ps.push("github"); if(!ps.length) ps.push("none");
      for (const p of ps) (groups[p]=groups[p]||[]).push(r) }
    const order = Object.keys(groups).sort((a,b)=>(a==="github")-(b==="github")||(a==="none")-(b==="none")||plab(a).localeCompare(plab(b)));
    for (const p of order){
      const label = p==="github"?"En GitHub":p==="none"?"Sin partidas":plab(p);
      h += `<div class="group"><h3><span class="dot" style="background:${PCOL[p]||"#666"}"></span>${esc(label)} <span class="hint">${groups[p].length} juegos</span></h3>${listHtml(groups[p])}</div>`;
    }
  } else h += listHtml(rows);
  el.innerHTML = h;
}
function statusFor(gid){ const g=S.games.find(x=>x.id===gid); return g||{} }
const ST_ORDER = ["conflicto","remoto-nuevo","solo-remoto","local-nuevo","solo-local","sincronizado","sin-ruta","vacio"];
function rowStatus(r){ const ids = r.game_ids || [r.game_id]; const gs = ids.map(statusFor);
  gs.sort((a,b)=>ST_ORDER.indexOf(a.status)-ST_ORDER.indexOf(b.status)); return gs[0]||{} }
function stClass(s){ return {sincronizado:"ok","solo-local":"up","local-nuevo":"up","solo-remoto":"down","remoto-nuevo":"down",conflicto:"bad"}[s]||"none" }
function stShort(s){ return {sincronizado:"✔ Al día","solo-local":"↑ Por subir","local-nuevo":"↑ Por subir","solo-remoto":"↓ Por bajar","remoto-nuevo":"↓ Por bajar",conflicto:"⚠ Conflicto",vacio:"—","sin-ruta":"Sin carpeta aquí"}[s]||s }
function listHtml(rows){
  let h = `<div class="list"><div class="row head"><div></div><div>Juego</div><div>Sistema</div><div>Dónde hay partida</div><div>En GitHub</div><div>Aquí</div></div>`;
  for (const r of rows){
    const g = rowStatus(r);
    const chips = [...new Map(r.where.map(w=>[w.device,w])).values()].map(w=>
      `<span class="pchip" title="${esc(w.device)}${w.modified?" · "+esc(fmtD(w.modified)):""}" style="border-color:${PCOL[w.platform]||"#666"};color:${PCOL[w.platform]||"#aaa"}"><span class="dot" style="background:${PCOL[w.platform]||"#666"}"></span>${esc(plab(w.platform))}${S.devices.filter(d=>d.platform===w.platform).length>1?" · "+esc(w.device):""}</span>`).join("");
    const gh = r.github ? `<div class="st">${esc(fmtD(r.github.updated_at))}</div><div class="sub">desde ${esc(r.github.device||"?")}</div>` : `<span class="hint">—</span>`;
    h += `<div class="row" onclick="openGame('${esc(r.game_id)}')">
      <div class="av" style="background:${sysColor(r.system)}">${esc(initials(r.name))}</div>
      <div style="min-width:0"><div class="name">${esc(r.name)}</div><div class="sub">${r.item?"en "+esc(r.parent):(g.local_path?esc(g.local_path):"sin carpeta en este dispositivo")}</div></div>
      <div><span class="badge">${esc(r.system||"—")}</span></div>
      <div class="chips">${chips||'<span class="hint">ningún dispositivo</span>'}</div>
      <div>${gh}</div>
      <div class="st ${stClass(g.status)}">${esc(stShort(g.status))}</div>
    </div>`;
  }
  return h + `</div>`;
}
function goTab(t){ tab=t; document.querySelectorAll("#nav button").forEach(x=>x.classList.toggle("on",x.dataset.tab===t)); render() }

/* ---------------- Detalle de un juego ---------------- */
function openGame(id){
  const g = S.games.find(x=>x.id===id); if (!g) return;
  const items = S.rows.filter(r=>r.item && (r.game_ids||[]).includes(id));
  let b = `<div class="kv">
    <div>Sistema</div><div>${esc(g.system||"—")}${g.type==="collection"?" · colección (varias partidas en una carpeta)":""}</div>
    <div>Estado aquí</div><div class="st ${stClass(g.status)}">${esc(g.status_text)}</div>
    <div>Carpeta aquí</div><div class="mono">${esc(g.local_path||"(sin carpeta)")}${g.override?' <span class="badge">solo este dispositivo</span>':""}</div>
    <div>Archivos incluidos</div><div class="mono">${esc((g.include||[]).join(", "))}${g.exclude&&g.exclude.length?" · excluye "+esc(g.exclude.join(", ")):""}</div>
    <div>En GitHub</div><div>${g.github?`${esc(fmtD(g.github.updated_at))} desde <b>${esc(g.github.device)}</b> (${g.github.files} archivos)`:"todavía no se subió"}</div>
    ${g.steam_id?`<div>Steam AppID</div><div>${g.steam_id}</div>`:""}
    ${g.notes?`<div>Notas</div><div>${esc(g.notes)}</div>`:""}
  </div>
  <h4 style="margin:18px 0 6px">Carpetas por plataforma</h4>
  <table><tr><th>Plataforma</th><th>Carpeta</th></tr>${Object.entries(g.paths||{}).map(([p,v])=>`<tr><td><span class="dot" style="background:${PCOL[p]||"#666"}"></span> ${esc(plab(p))}</td><td class="mono">${esc(v)}</td></tr>`).join("")||'<tr><td colspan=2 class="hint">ninguna</td></tr>'}</table>
  <h4 style="margin:18px 0 6px">Dispositivos con partida</h4>
  <table><tr><th>Dispositivo</th><th>Archivos</th><th>Tamaño</th><th>Modificado</th></tr>${g.where.map(w=>`<tr><td><span class="dot" style="background:${PCOL[w.platform]||"#666"}"></span> ${esc(w.device)} <span class="hint">${esc(plab(w.platform))}${w.this?" · este":""}</span></td><td>${w.files}</td><td>${fmtB(w.bytes)}</td><td>${esc(fmtD(w.modified))}</td></tr>`).join("")||'<tr><td colspan=4 class="hint">ninguno</td></tr>'}</table>
  ${items.length?`<h4 style="margin:18px 0 6px">Juegos dentro (${items.length})</h4><div class="items">${items.map(i=>`<span class="badge" title="${esc(i.system)}">${esc(i.name)}</span>`).join("")}</div>`:""}
  <h4 style="margin:18px 0 6px">Cambiar carpeta en este dispositivo</h4>
  <div class="inline"><input type="text" id="gPath" value="${esc(g.local_path||"")}" placeholder="Carpeta de partidas"><button class="btn small" onclick="browse('gPath')">Explorar…</button></div>
  <label class="chk" style="margin-top:8px"><input type="checkbox" id="gShared" ${g.override?"":"checked"}> Guardar para todos los dispositivos ${esc(S.platform_label)}</label>
  <div style="margin-top:8px"><button class="btn small" onclick="savePath('${esc(id)}')">Guardar carpeta</button></div>`;
  modal(g.name, b, `
    <button class="btn danger small" onclick="removeGame('${esc(id)}')">Quitar del catálogo</button>
    <button class="btn small" onclick="history_('${esc(id)}')">Historial</button>
    <div class="spacer"></div>
    <button class="btn small" onclick="syncGames(['${esc(id)}'],'pull')" ${g.github?"":"disabled"}>↓ Bajar de GitHub</button>
    <button class="btn small" onclick="syncGames(['${esc(id)}'],'push')" ${g.local_path?"":"disabled"}>↑ Subir esta</button>
    <button class="btn primary small" onclick="syncGames(['${esc(id)}'])">⇅ Sincronizar</button>`);
}
async function savePath(id){
  const j = await api("/api/set-path",{id, path:$("#gPath").value, shared:$("#gShared").checked},"Guardando…");
  if (j.ok){ toast("Carpeta guardada"); closeModal(); refresh() }
}
async function removeGame(id){
  const g = S.games.find(x=>x.id===id);
  modal("Quitar "+g.name, `<p>Se quitará <b>${esc(g.name)}</b> de la lista de juegos sincronizados en todos tus dispositivos. Tus partidas locales <b>no</b> se borran.</p>
    <label class="chk"><input type="checkbox" id="delRemote"> Borrar también sus partidas del repositorio (quedan en el historial de git)</label>`,
    `<button class="btn" onclick="closeModal()">Cancelar</button><button class="btn danger" id="okRm">Quitar</button>`);
  $("#okRm").onclick = async () => { const j = await api("/api/remove",{id, delete_remote:$("#delRemote").checked},"Quitando…");
    if (j.ok){ closeModal(); toast("Quitado"); refresh() } };
}
async function history_(id){
  const j = await api("/api/history",{id},"Leyendo historial…"); if (!j.ok) return;
  const g = S.games.find(x=>x.id===id);
  modal("Historial · "+g.name, j.commits.length ? `<p class="hint">Restaurar reemplaza la partida de este dispositivo por esa versión (antes se hace un backup local).</p><table><tr><th>Versión</th><th>Fecha</th><th>Detalle</th><th></th></tr>${j.commits.map(c=>`<tr><td class="mono">${esc(c.commit)}</td><td>${esc(c.date)}</td><td>${esc(c.message)}</td><td><button class="btn small" onclick="restore('${esc(id)}','${esc(c.commit)}')">Restaurar</button></td></tr>`).join("")}</table>` : `<p>Sin versiones en GitHub todavía.</p>`,
    `<button class="btn" onclick="openGame('${esc(id)}')">Volver</button>`);
}
async function restore(id, commit){
  if (!confirm("¿Reemplazar la partida de este dispositivo por la versión "+commit+"?")) return;
  const j = await api("/api/restore",{id, commit},"Restaurando…");
  if (j.ok){ closeModal(); toast("Versión restaurada. Usa 'Subir esta' si quieres que sea la de GitHub."); refresh() }
}
async function syncGames(ids, mode){
  const j = await api("/api/sync",{games:ids, mode:mode||"auto"}, mode==="pull"?"Descargando…":mode==="push"?"Subiendo…":"Sincronizando…");
  if (!j.ok) return;
  closeModal();
  if (/CONFLICTO/.test(j.log||"")){ await refresh(); showConflicts(); return }
  toast(/subiendo|descargando/i.test(j.log||"") ? "Sincronización completa" : "Todo estaba al día");
  refresh();
}
function showConflicts(){
  const cs = S.games.filter(g=>g.status==="conflicto");
  if (!cs.length) return;
  modal("Conflictos", `<p>Estos juegos cambiaron en este dispositivo <b>y</b> en otro. Elige qué versión conservar (la otra queda en el historial / backups).</p>
    <table><tr><th>Juego</th><th>En GitHub</th><th></th></tr>${cs.map(g=>`<tr><td><b>${esc(g.name)}</b></td><td>${g.github?esc(fmtD(g.github.updated_at))+" desde "+esc(g.github.device):""}</td>
    <td style="white-space:nowrap"><button class="btn small" onclick="syncGames(['${esc(g.id)}'],'push')">Usar la de aquí</button> <button class="btn small" onclick="syncGames(['${esc(g.id)}'],'pull')">Usar la de GitHub</button></td></tr>`).join("")}</table>`,
    `<button class="btn" onclick="closeModal()">Más tarde</button>`);
}

/* ---------------- Detectar ---------------- */
function detectView(){
  const db = S.db||{};
  const dbTxt = S.platform==="android" ? "En Android se buscan carpetas de emuladores (los juegos de PC no aplican)." :
    db.exists ? `Base de datos de juegos de PC: ${db.games.toLocaleString("es")} juegos · actualizada ${esc(db.updated)}${db.stale?" (se actualizará al buscar)":""}` :
    "La primera búsqueda descarga la base de datos de Ludusavi (~18 MB) con las rutas de guardado de miles de juegos de PC.";
  let h = `<div class="card"><h2>Detectar partidas en ${esc(S.device)}</h2>
    <p>Busca en las carpetas donde normalmente se guardan las partidas: juegos de PC (también dentro de Proton en Steam Deck/Linux), RetroArch, PCSX2, DuckStation, PPSSPP, Dolphin, Ryujinx, yuzu y derivados, Citra/Azahar, Cemu, RPCS3 y EmuDeck.</p>
    <div class="inline"><button class="btn primary" onclick="doScan()">🔍 Buscar partidas</button>
    ${S.platform!=="android"?`<button class="btn" onclick="updDb()">Actualizar base de datos</button>`:""}<span class="hint">${dbTxt}</span></div></div>`;
  if (scanResults){
    if (!scanResults.length) return h + `<div class="card"><p>No se encontraron partidas en las ubicaciones conocidas. Puedes agregarlas a mano en <a href="#" onclick="goTab('add');return false">Agregar manual</a>.</p></div>`;
    const lab = {new:["Nuevo","var(--ok)"],merge:["Añadir carpeta","var(--accent2)"],known:["Ya agregado","var(--muted)"]};
    h += `<div class="card"><div class="inline" style="margin-bottom:10px"><b>${scanResults.length} encontrados</b><div class="spacer"></div>
      <label class="chk"><input type="checkbox" id="scanSync" checked> Subir a GitHub después</label>
      <button class="btn primary" onclick="addDetected()" ${scanSel.size?"":"disabled"}>Agregar seleccionados (${scanSel.size})</button></div>
      <table><tr><th><input type="checkbox" onchange="selAll(this.checked)"></th><th>Juego</th><th>Sistema</th><th>Archivos</th><th>Modificado</th><th>Estado</th></tr>
      ${scanResults.map((c,i)=>`<tr><td>${c.status==="known"?"":`<input type="checkbox" ${scanSel.has(i)?"checked":""} onchange="togSel(${i},this.checked)">`}</td>
        <td><b>${esc(c.name)}</b>${c.steam_id?` <span class="hint">· Steam ${c.steam_id}</span>`:""}<div class="path">${esc(c.path)}</div>${c.items&&c.items.length?`<div class="hint">${esc(c.items.slice(0,8).join(" · "))}${c.items.length>8?" …":""}</div>`:""}</td>
        <td><span class="badge">${esc(c.system)}</span></td><td>${c.files}<div class="hint">${fmtB(c.bytes)}</div></td><td>${esc(c.modified)}</td>
        <td style="color:${lab[c.status][1]}">${lab[c.status][0]}${c.existing_id&&c.status==="merge"?`<div class="hint">→ ${esc(c.existing_id)}</div>`:""}</td></tr>`).join("")}</table></div>`;
  }
  return h;
}
async function doScan(){
  const j = await api("/api/scan",{update:true},"Buscando partidas… (la primera vez puede tardar un poco)");
  if (!j.ok) return;
  scanResults = j.candidates; scanSel = new Set(scanResults.map((c,i)=>c.status!=="known"?i:-1).filter(i=>i>=0));
  const s = await api("/api/state"); if (s.ok) S = s;
  render();
}
async function updDb(){ const j = await api("/api/update-db",{},"Descargando base de datos…"); if (j.ok){ toast(`Base de datos: ${j.count} juegos`); refresh() } }
function togSel(i,on){ on?scanSel.add(i):scanSel.delete(i); render() }
function selAll(on){ scanSel = new Set(on?scanResults.map((c,i)=>c.status!=="known"?i:-1).filter(i=>i>=0):[]); render() }
async function addDetected(){
  const cands = [...scanSel].map(i=>scanResults[i]);
  const j = await api("/api/add-detected",{candidates:cands, sync:$("#scanSync").checked},"Agregando y subiendo…");
  if (!j.ok) return;
  toast(`${cands.length} juegos agregados`); scanResults=null; scanSel=new Set(); goTab("games"); refresh();
}

/* ---------------- Agregar manual ---------------- */
function addView(){
  const f = addForm;
  const systems = [...new Set(["PC","GBA","GB/GBC","NES","SNES","N64","NDS","3DS","GameCube","Wii","Wii U","Switch","PS1","PS2","PS3","PSP","Mega Drive","Dreamcast","Arcade","Emulador",...S.rows.map(r=>r.system)].filter(Boolean))];
  return `<div class="card"><h2>Agregar un juego a mano</h2><p>Para juegos o emuladores que la detección no encuentra. Indica la carpeta donde se guardan las partidas en este dispositivo; en los demás dispositivos podrás indicar la suya desde «Mis partidas».</p>
  <div class="form">
    <label>Nombre</label><input type="text" id="fName" value="${esc(f.name)}" placeholder="Ej: Hollow Knight">
    <label>Sistema</label><div class="inline"><input type="text" id="fSystem" list="sysList" value="${esc(f.system)}"><datalist id="sysList">${systems.map(s=>`<option value="${esc(s)}">`).join("")}</datalist></div>
    <label>Tipo</label><select id="fType"><option value="game" ${f.type==="game"?"selected":""}>Un juego</option><option value="collection" ${f.type==="collection"?"selected":""}>Carpeta de emulador (varios juegos dentro)</option></select>
    <label>Carpeta de partidas</label><div class="inline"><input type="text" id="fPath" value="${esc(f.path)}" placeholder="Ej: %APPDATA%\MiJuego  o  ~/.local/share/MiJuego"><button class="btn" onclick="browse('fPath')">Explorar…</button></div>
    <label>Incluir archivos</label><div><input type="text" id="fInclude" value="${esc(f.include)}" style="width:100%"><div class="hint">Patrones separados por coma. <span class="mono">*</span> = todo · <span class="mono">*.sav, *.dat</span> · <span class="mono">*/ER0000.*</span></div></div>
    <label>Excluir</label><input type="text" id="fExclude" value="${esc(f.exclude)}" placeholder="Ej: *.log, cache/*">
    <div class="full"><label class="chk"><input type="checkbox" id="fShared" ${f.shared?"checked":""}> Guardar esta carpeta para todos los dispositivos ${esc(S.platform_label)}</label></div>
    <div class="full"><label class="chk"><input type="checkbox" id="fSync" ${f.sync?"checked":""}> Subir la partida a GitHub al agregar</label></div>
    <div class="full inline"><button class="btn" onclick="preview()">Vista previa</button><button class="btn primary" onclick="addManual()">Agregar juego</button></div>
    <div class="full" id="prev"></div>
  </div></div>`;
}
function readAdd(){
  const f = addForm;
  f.name=$("#fName").value; f.system=$("#fSystem").value; f.type=$("#fType").value; f.path=$("#fPath").value;
  f.include=$("#fInclude").value; f.exclude=$("#fExclude").value; f.shared=$("#fShared").checked; f.sync=$("#fSync").checked;
  const split = s => s.split(",").map(x=>x.trim()).filter(Boolean);
  return {name:f.name, system:f.system, type:f.type, item_mode:"files", path:f.path, include:split(f.include), exclude:split(f.exclude), shared:f.shared, sync:f.sync};
}
function bindAdd(){ ["#fName","#fSystem","#fType","#fPath","#fInclude","#fExclude","#fShared","#fSync"].forEach(s=>{const e=$(s); if(e) e.addEventListener("change",readAdd)}) }
async function preview(){
  const b = readAdd(); const j = await api("/api/preview", b); if (!j.ok) return;
  $("#prev").innerHTML = !j.exists ? `<div class="warnbox">La carpeta no existe en este dispositivo: ${esc(j.path)}</div>` :
    `<div class="card" style="margin:0"><b>${j.files} archivos</b> · ${fmtB(j.bytes)} <span class="hint mono">${esc(j.path)}</span>
     <div class="mono hint" style="margin-top:8px">${j.sample.map(esc).join("<br>")}${j.files>j.sample.length?"<br>…":""}</div>
     ${j.items?`<div style="margin-top:8px" class="items">${j.items.map(i=>`<span class="badge">${esc(i)}</span>`).join("")}</div>`:""}</div>`;
}
async function addManual(){
  const b = readAdd();
  const j = await api("/api/add", b, b.sync?"Agregando y subiendo…":"Agregando…");
  if (!j.ok) return;
  addForm = {name:"",id:"",system:"PC",type:"game",item_mode:"files",path:"",include:"*",exclude:"",shared:true,sync:true};
  toast("Juego agregado"); goTab("games"); refresh();
}

/* ---------------- Explorador de carpetas ---------------- */
let browseTarget = null, prevModal = null;
async function browse(target, path){
  browseTarget = target;
  const start = path ?? ($("#"+target) ? $("#"+target).value : "");
  if (path===undefined && $("#modal").classList.contains("on")) prevModal = {t:$("#mTitle").innerHTML,b:$("#mBody").innerHTML,f:$("#mFoot").innerHTML,v:$("#"+target).value};
  const j = await api("/api/browse?path="+encodeURIComponent(start||"")); if (!j.ok) return;
  modal("Elegir carpeta", `<div class="sc">${j.shortcuts.map(s=>`<button class="btn small" onclick='browse(${JSON.stringify(target)},${JSON.stringify(s.path)})'>${esc(s.label)}</button>`).join("")}</div>
    <div class="inline" style="margin-bottom:10px"><input type="text" id="bPath" value="${esc(j.path)}" onkeydown="if(event.key==='Enter')browse(browseTarget,this.value)"><button class="btn small" onclick="browse(browseTarget,$('#bPath').value)">Ir</button></div>
    ${j.error?`<div class="warnbox">${esc(j.error)}</div>`:""}
    <div class="browser-list">${j.parent?`<div onclick='browse(${JSON.stringify(target)},${JSON.stringify(j.parent)})'>⬆ ..</div>`:""}
    ${j.dirs.map(d=>`<div onclick='browse(${JSON.stringify(target)},${JSON.stringify(j.path.replace(/[\\/]+$/,"")+(j.path.includes("\\")?"\\":"/")+d)})'>📁 ${esc(d)}</div>`).join("")}
    ${(j.files||[]).slice(0,60).map(f=>`<div class="f">📄 ${esc(f)}</div>`).join("")}</div>`,
    `<button class="btn" onclick="cancelBrowse()">Cancelar</button><button class="btn primary" onclick='pickDir(${JSON.stringify(j.path)})'>Usar esta carpeta</button>`);
}
function restorePrev(){ if (prevModal){ $("#mTitle").innerHTML=prevModal.t; $("#mBody").innerHTML=prevModal.b; $("#mFoot").innerHTML=prevModal.f; return true } return false }
function cancelBrowse(){ const v = prevModal && prevModal.v; if (restorePrev()){ $("#"+browseTarget).value=v; prevModal=null } else closeModal() }
function pickDir(p){ if (restorePrev()){ prevModal=null } else closeModal(); const e=$("#"+browseTarget); if (e){ e.value=p; e.dispatchEvent(new Event("change")) } }

/* ---------------- Ajustes / configuración ---------------- */
function settingsView(){
  return `<div class="card"><h2>Este dispositivo</h2><div class="form">
    <label>Nombre</label><div class="inline"><input type="text" id="sDev" value="${esc(S.device)}"><button class="btn" onclick="saveDev()">Guardar</button></div>
    <label>Plataforma</label><div>${esc(S.platform_label)}</div>
    <label>Repositorio</label><div class="mono">${esc(S.repo_url||"")}</div>
    <label>Datos de SaveSync</label><div class="mono">${esc(S.home)}</div>
    <label>Versión</label><div>${esc(S.version)}</div></div></div>
    <div class="card"><h2>Dispositivos</h2><table><tr><th>Dispositivo</th><th>Plataforma</th><th>Juegos con partida</th><th>Último reporte</th></tr>
    ${S.devices.map(d=>`<tr><td>${esc(d.device)}${d.this?" <span class='hint'>(este)</span>":""}</td><td><span class="dot" style="background:${PCOL[d.platform]||"#666"}"></span> ${esc(plab(d.platform))}</td><td>${d.games}</td><td>${esc(fmtD(d.updated_at))}</td></tr>`).join("")}</table></div>`;
}
async function saveDev(){ const j = await api("/api/config",{device_name:$("#sDev").value},"Guardando…"); if (j.ok){ toast("Guardado"); refresh() } }
function setupView(){
  return `<div class="card" style="max-width:720px;margin:30px auto"><h2>Conectar con GitHub</h2>
    <p>Crea en GitHub un repositorio <b>privado y vacío</b> (por ejemplo <span class="mono">mis-partidas</span>) y pega aquí su dirección. Usa la misma en todos tus dispositivos.</p>
    <div class="form"><label>URL del repositorio</label><input type="text" id="suRepo" placeholder="https://github.com/usuario/mis-partidas.git">
    <label>Nombre de este dispositivo</label><input type="text" id="suDev" value="${esc(S.device)}">
    <div class="full"><button class="btn primary" onclick="doSetup()">Conectar</button></div></div>
    <p class="hint" style="margin-top:14px">Si es la primera vez que usas git aquí, puede abrirse una ventana para iniciar sesión en GitHub.</p></div>`;
}
async function doSetup(){ const j = await api("/api/setup",{repo:$("#suRepo").value, device:$("#suDev").value},"Conectando con GitHub…"); if (j.ok){ toast("Conectado"); refresh() } }

/* ---------------- Modal ---------------- */
function modal(title, body, foot){ $("#mTitle").innerHTML=esc(title); $("#mBody").innerHTML=body; $("#mFoot").innerHTML=foot||""; $("#modal").classList.add("on"); prevModal=prevModal }
function closeModal(){ $("#modal").classList.remove("on"); prevModal=null }
$("#modal").addEventListener("click", e => { if (e.target.id==="modal") closeModal() });
document.addEventListener("keydown", e => { if (e.key==="Escape") closeModal() });

refresh(true).then(async () => {
  const j = await fetch("/api/update",{headers:{"X-Token":TOKEN}}).then(r=>r.json()).catch(()=>null);
  if (j && j.update){ const a=document.createElement("a"); a.href=j.update.url; a.target="_blank"; a.className="devchip";
    a.style.cssText="border-color:var(--accent);color:var(--text);text-decoration:none"; a.textContent="⬆ Nueva versión "+j.update.version;
    $("#devchip").after(a) }
});
</script>
</body>
</html>
"""
