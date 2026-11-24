"""SaveSync - aplicación (Flet) para Windows, Linux / Steam Deck y Android."""
import os
import string
import time
from pathlib import Path

import flet as ft

import savesync_core as core
import savesync_detect as det

ACCENT = "#7C5CFF"
PCOL = {"windows": "#3B82F6", "steamdeck": "#B26BFF", "linux": "#F59E0B", "android": "#22C55E",
        "macos": "#9CA3AF", "github": "#CFD5E2"}
SYSCOL = ["#7C5CFF", "#5B8CFF", "#2FBF71", "#F59E0B", "#EF5B5B", "#14B8A6", "#E879F9", "#F97316", "#84CC16", "#06B6D4"]
STATUS_SHORT = {"sincronizado": ("Al día", ft.Icons.CHECK_CIRCLE, "#2FBF71"),
                "solo-local": ("Por subir", ft.Icons.ARROW_UPWARD, "#5B8CFF"),
                "local-nuevo": ("Por subir", ft.Icons.ARROW_UPWARD, "#5B8CFF"),
                "solo-remoto": ("Por bajar", ft.Icons.ARROW_DOWNWARD, "#5B8CFF"),
                "remoto-nuevo": ("Por bajar", ft.Icons.ARROW_DOWNWARD, "#5B8CFF"),
                "conflicto": ("Conflicto", ft.Icons.WARNING_AMBER, "#F2B441"),
                "sin-ruta": ("Sin carpeta aquí", ft.Icons.FOLDER_OFF_OUTLINED, "#8B93A7"),
                "vacio": ("—", ft.Icons.REMOVE, "#8B93A7")}
STATUS_LONG = {"sin-ruta": "Sin carpeta en este dispositivo", "vacio": "No hay partidas ni aquí ni en GitHub",
               "sincronizado": "Sincronizado", "solo-local": "Solo existe aquí (se subirá)",
               "solo-remoto": "Solo existe en GitHub (se descargará)", "remoto-nuevo": "Hay una versión más nueva en GitHub",
               "local-nuevo": "La versión de aquí es más nueva", "conflicto": "Conflicto: cambió aquí y en otro dispositivo"}
ST_ORDER = ["conflicto", "remoto-nuevo", "solo-remoto", "local-nuevo", "solo-local", "sincronizado", "sin-ruta", "vacio"]
SYSTEMS = ["PC", "GBA", "GB/GBC", "NES", "SNES", "N64", "NDS", "3DS", "GameCube", "Wii", "Wii U", "Switch",
           "PS1", "PS2", "PS3", "PSP", "Mega Drive", "Dreamcast", "Arcade", "Emulador"]


def plab(p):
    return "En GitHub" if p == "github" else core.PLATFORM_LABELS.get(p, p)


def sys_color(s):
    h = 0
    for ch in str(s):
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return SYSCOL[h % len(SYSCOL)]


def initials(name):
    words = [w for w in "".join(c if c.isalnum() else " " for c in str(name)).split() if w]
    return ((words[0][0] if words else "?") + (words[1][0] if len(words) > 1 else "")).upper()


def fmt_date(d):
    return (d or "")[:16].replace("T", " ")


# ---------------------------------------------------------------------------
# Modelo de la vista "Mis partidas"
# ---------------------------------------------------------------------------
def build_view(ss):
    """Junta catálogo + reportes de dispositivos + GitHub en filas listas para mostrar."""
    games = ss.load_games()
    live = ss.build_report(games)
    reports = [r for r in ss.load_reports() if r.get("device") != ss.device]
    reports.append({"device": ss.device, "platform": ss.platform, "games": live,
                    "updated_at": core.now_iso(), "this": True})
    devices = [{"device": r.get("device"), "platform": r.get("platform"), "updated_at": r.get("updated_at"),
                "this": bool(r.get("this")), "games": len(r.get("games", {}))} for r in reports]
    ginfo, rows, merged = {}, [], {}
    for g in games:
        a = ss.analyze(g)
        m = a["manifest"] or {}
        gh = {"device": m.get("device"), "updated_at": m.get("updated_at"),
              "files": len(a["remote_shas"])} if a["remote_hash"] else None
        where = [dict(device=r["device"], platform=r["platform"], this=bool(r.get("this")),
                      **{k: v for k, v in r["games"][g["id"]].items() if k != "items"})
                 for r in reports if g["id"] in r.get("games", {})]
        gi = dict(g, status=a["status"], local_path=str(a["path"]) if a["path"] else None,
                  override=ss.config.get("path_overrides", {}).get(g["id"]), github=gh, where=where)
        ginfo[g["id"]] = gi
        if g.get("type") == "collection":
            items = {}
            for r in reports:
                for it in r.get("games", {}).get(g["id"], {}).get("items", []):
                    items.setdefault((it["name"], it.get("system", "")), {"where": [], "github": False})
                    items[(it["name"], it.get("system", ""))]["where"].append(
                        {"device": r["device"], "platform": r["platform"], "this": bool(r.get("this"))})
            if a["remote_hash"]:
                for it in core.collection_items(a["remote_shas"].keys(), g):
                    items.setdefault((it["name"], it.get("system", "")), {"where": [], "github": False})
                    items[(it["name"], it.get("system", ""))]["github"] = True
            for (name, system), v in items.items():
                key = "item::" + name.lower()
                r = merged.get(key)
                if r is None:
                    r = merged[key] = {"key": key, "name": name, "system": "", "game_ids": [], "item": True,
                                       "parents": [], "where": [], "github": None}
                    rows.append(r)
                if system and (not r["system"] or r["system"] == "Emulador"):
                    r["system"] = system
                if g["id"] not in r["game_ids"]:
                    r["game_ids"].append(g["id"])
                    r["parents"].append(g["name"])
                known = {w["device"] for w in r["where"]}
                r["where"] += [w for w in v["where"] if w["device"] not in known]
                if v["github"] and r["github"] is None:
                    r["github"] = gh
            if not items:
                rows.append({"key": g["id"], "name": g["name"], "system": g.get("system", ""),
                             "game_ids": [g["id"]], "item": False, "where": [], "github": None, "empty": True})
        else:
            rows.append({"key": g["id"], "name": g["name"], "system": g.get("system", ""), "game_ids": [g["id"]],
                         "item": False, "where": where, "github": gh, "empty": not where and not gh})
    for r in rows:
        if r.get("item"):
            r["parent"] = ", ".join(r.pop("parents"))
            r["system"] = r["system"] or "Emulador"
        sts = sorted((ginfo[i]["status"] for i in r["game_ids"]), key=ST_ORDER.index)
        r["status"] = sts[0] if sts else "vacio"
    return {"games": ginfo, "rows": rows, "devices": devices}


# ---------------------------------------------------------------------------
# Aplicación
# ---------------------------------------------------------------------------
class App:
    def __init__(self, page: ft.Page):
        self.page = page
        self.logs = []
        self.ss = core.SaveSync(log=self.log)
        self.view = None
        self.filters = {"plat": "all", "sys": "", "q": "", "group": False, "empty": False}
        self.tab = 0
        self.scan_results = None
        self.scan_sel = set()
        self.busy_dialog = None
        self.update_info = None
        self.add_form = {}
        self.perm_ok = True

        page.title = "SaveSync"
        page.theme_mode = ft.ThemeMode.DARK
        page.theme = ft.Theme(color_scheme_seed=ACCENT, use_material3=True)
        page.dark_theme = ft.Theme(color_scheme_seed=ACCENT, use_material3=True)
        page.bgcolor = "#0D0F14"
        page.padding = 0
        try:
            page.window.min_width = 380
            page.window.min_height = 560
            if page.window.width and page.window.width < 1000:
                page.window.width, page.window.height = 1100, 760
        except Exception:
            pass
        page.on_resize = lambda e: self.layout()
        self.content = ft.Container(expand=True, padding=ft.Padding.only(left=20, right=20, top=12, bottom=12))
        self.progress = ft.ProgressBar(visible=False, color=ACCENT)
        self.layout()
        self.route()
        if core.is_android():
            self.page.run_task(self.check_android_permission)
        self.page.run_thread(self._check_update)

    # --- utilidades de interfaz -------------------------------------------
    @property
    def narrow(self):
        return (self.page.width or 1000) < 760

    def log(self, msg):
        self.logs.append("{}  {}".format(time.strftime("%H:%M:%S"), msg))
        self.logs = self.logs[-400:]

    def toast(self, msg, error=False):
        self.page.show_dialog(ft.SnackBar(ft.Text(msg), bgcolor="#5A2323" if error else None,
                                          duration=6000 if error else 3000))

    def close_dialog(self, e=None):
        try:
            self.page.pop_dialog()
        except Exception:
            pass

    def dialog(self, title, content, actions, scrollable=True):
        dlg = ft.AlertDialog(title=ft.Text(title), content=content, actions=actions, scrollable=scrollable,
                             actions_alignment=ft.MainAxisAlignment.END)
        self.page.show_dialog(dlg)
        return dlg

    def run_bg(self, label, fn, done=None, quiet=False):
        """Ejecuta fn() en segundo plano con indicador de progreso."""
        def worker():
            self.progress.visible = True
            if label and not quiet:
                self.busy_dialog = ft.AlertDialog(modal=True, content=ft.Row(
                    [ft.ProgressRing(width=28, height=28), ft.Text(label, expand=True)], spacing=16, tight=True))
                self.page.show_dialog(self.busy_dialog)
            self.page.update()
            try:
                res = fn()
                err = None
            except core.AuthError as e:
                res, err = None, e
                self.ss.logout()
            except core.SaveSyncError as e:
                res, err = None, e
            except Exception as e:  # noqa
                res, err = None, e
                self.log("ERROR {}: {}".format(type(e).__name__, e))
            finally:
                self.progress.visible = False
                if self.busy_dialog is not None:
                    self.close_dialog()
                    self.busy_dialog = None
            if err is not None:
                self.toast(str(err), True)
                if isinstance(err, core.AuthError):
                    self.route()
            elif done:
                done(res)
            self.page.update()
        self.page.run_thread(worker)

    async def copy(self, text):
        await ft.Clipboard().set(text)
        self.toast("Copiado")

    async def open_url(self, url):
        await ft.UrlLauncher().launch_url(url)

    def _check_update(self):
        self.update_info = core.update_available()
        if self.update_info:
            self.layout()
            self.page.update()

    # --- estructura ---------------------------------------------------------
    DESTS = [("Partidas", ft.Icons.SPORTS_ESPORTS_OUTLINED, ft.Icons.SPORTS_ESPORTS),
             ("Detectar", ft.Icons.TRAVEL_EXPLORE_OUTLINED, ft.Icons.TRAVEL_EXPLORE),
             ("Agregar", ft.Icons.ADD_CIRCLE_OUTLINE, ft.Icons.ADD_CIRCLE),
             ("Ajustes", ft.Icons.SETTINGS_OUTLINED, ft.Icons.SETTINGS)]

    def on_nav(self, e):
        self.tab = int(e.control.selected_index)
        self.render()

    def layout(self):
        p = self.page
        p.controls.clear()
        main_ui = self.ss.configured
        header = ft.Container(
            padding=ft.Padding.symmetric(horizontal=20, vertical=10), bgcolor="#121521",
            content=ft.Row([
                ft.Container(ft.Icon(ft.Icons.SWAP_VERT, color="white", size=20), width=34, height=34,
                             border_radius=10, alignment=ft.Alignment.CENTER,
                             gradient=ft.LinearGradient(colors=[ACCENT, "#5B8CFF"])),
                ft.Text("SaveSync", size=20, weight=ft.FontWeight.BOLD),
                ft.Container(ft.Text("{} · {}".format(self.ss.device, plab(self.ss.platform)), size=12,
                                     color="#AAB1C3"), padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                             border_radius=20, bgcolor="#1B2030", visible=not self.narrow),
                ft.Container(expand=True),
                ft.TextButton("Nueva versión " + self.update_info["version"], icon=ft.Icons.NEW_RELEASES,
                              on_click=lambda e: self.page.run_task(self.open_url, self.update_info["url"]))
                if self.update_info else ft.Container(),
                ft.IconButton(ft.Icons.REFRESH, tooltip="Actualizar", on_click=lambda e: self.refresh(True),
                              visible=main_ui),
                ft.FilledButton("Sincronizar todo" if not self.narrow else "Sincronizar", icon=ft.Icons.SYNC,
                                on_click=lambda e: self.sync([]), visible=main_ui),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=12))
        if main_ui and not self.narrow:
            rail = ft.NavigationRail(
                selected_index=self.tab, on_change=self.on_nav, label_type=ft.NavigationRailLabelType.ALL,
                bgcolor="#0F121A", min_width=84, group_alignment=-0.9,
                destinations=[ft.NavigationRailDestination(icon=i, selected_icon=si, label=l)
                              for l, i, si in self.DESTS])
            body = ft.Row([rail, ft.VerticalDivider(width=1, color="#262C3D"), self.content],
                          expand=True, spacing=0, vertical_alignment=ft.CrossAxisAlignment.STRETCH)
            p.navigation_bar = None
        else:
            body = self.content
            p.navigation_bar = ft.NavigationBar(
                selected_index=self.tab, on_change=self.on_nav, bgcolor="#0F121A",
                destinations=[ft.NavigationBarDestination(icon=i, selected_icon=si, label=l)
                              for l, i, si in self.DESTS]) if main_ui else None
        p.add(ft.Column([header, self.progress, body], expand=True, spacing=0))
        self.render()

    def route(self):
        if self.ss.configured:
            self.layout()
            self.refresh(True)
        else:
            self.layout()

    def render(self):
        if not self.ss.logged_in:
            self.content.content = self.login_view()
        elif not self.ss.configured:
            self.content.content = self.repo_view()
        elif self.tab == 0:
            self.content.content = self.games_view()
        elif self.tab == 1:
            self.content.content = self.detect_view()
        elif self.tab == 2:
            self.content.content = self.add_view()
        else:
            self.content.content = self.settings_view()
        self.page.update()

    def refresh(self, pull=True):
        def work():
            with self.ss.lock:
                self.ss.store.snapshot(refresh=pull)
                return build_view(self.ss)

        def done(v):
            self.view = v
            if self.ss.store.offline:
                self.toast("Sin conexión: se muestran los datos guardados.", True)
            self.render()
        self.run_bg(None, work, done, quiet=True)

    # --- conexión -----------------------------------------------------------
    def card(self, controls, padding=20):
        return ft.Container(ft.Column(controls, spacing=12, tight=True), padding=padding, border_radius=14,
                            bgcolor="#151922", border=ft.Border.all(1, "#262C3D"))

    def login_view(self):
        token_field = ft.TextField(label="Token de GitHub", password=True, can_reveal_password=True,
                                   on_submit=lambda e: login_token())
        code_area = ft.Column(tight=True, spacing=10)

        def login_token():
            t = (token_field.value or "").strip()
            if not t:
                return self.toast("Pega el token primero.", True)
            self.run_bg("Conectando…", lambda: self.ss.login_with_token(t),
                        lambda u: (self.toast("Conectado como " + u.get("login", "")), self.render()))

        def start_device(e):
            def work():
                return core.device_flow_start()

            def done(flow):
                code_area.controls = [
                    ft.Text("1. Abre github.com/login/device", size=15),
                    ft.Text("2. Escribe este código:", size=15),
                    ft.Row([ft.Text(flow["user_code"], size=34, weight=ft.FontWeight.BOLD, selectable=True,
                                    font_family="monospace"),
                            ft.IconButton(ft.Icons.COPY, tooltip="Copiar código",
                                          on_click=lambda e: self.page.run_task(self.copy, flow["user_code"]))]),
                    ft.Button("Abrir GitHub", icon=ft.Icons.OPEN_IN_NEW,
                              on_click=lambda e: self.page.run_task(self.open_url, flow["verification_uri"])),
                    ft.Row([ft.ProgressRing(width=18, height=18), ft.Text("Esperando que autorices…")])]
                self.page.update()
                self.page.run_thread(poll, flow)
            self.run_bg("Pidiendo código a GitHub…", work, done)

        def poll(flow):
            interval = int(flow.get("interval", 5))
            deadline = time.time() + int(flow.get("expires_in", 900))
            while time.time() < deadline and not self.ss.logged_in:
                time.sleep(interval)
                try:
                    st, token = core.device_flow_poll(flow["device_code"])
                except core.SaveSyncError:
                    continue
                if st == "ok":
                    try:
                        u = self.ss.login_with_token(token, "oauth")
                        self.toast("Conectado como " + u.get("login", ""))
                    except core.SaveSyncError as ex:
                        self.toast(str(ex), True)
                    self.render()
                    return
                if st == "slow_down":
                    interval += 5
                if st in ("expired", "denied"):
                    code_area.controls = [ft.Text("El código expiró o se canceló. Vuelve a intentarlo.",
                                                  color="#F2B441")]
                    self.page.update()
                    return

        parts = [ft.Text("Conecta tu cuenta de GitHub", size=24, weight=ft.FontWeight.BOLD),
                 ft.Text("SaveSync guarda tus partidas en un repositorio privado de tu cuenta. "
                         "No necesitas instalar Git.", color="#AAB1C3")]
        if core.OAUTH_CLIENT_ID:
            parts += [ft.FilledButton("Conectar con GitHub", icon=ft.Icons.LOGIN, on_click=start_device,
                                      height=48), code_area, ft.Divider(color="#262C3D"),
                      ft.Text("O usa un token personal:", color="#AAB1C3")]
        parts += [token_field,
                  ft.Text("GitHub > Settings > Developer settings > Personal access tokens > Fine-grained tokens. "
                          "Dale acceso a tu repositorio de partidas con permiso «Contents: Read and write». "
                          "Para que SaveSync pueda crear el repositorio por ti, usa un token «classic» con "
                          "permiso «repo».", size=12, color="#8B93A7"),
                  ft.Row([ft.Button("Conectar con token", icon=ft.Icons.KEY, on_click=lambda e: login_token()),
                          ft.TextButton("Crear token", icon=ft.Icons.OPEN_IN_NEW, on_click=lambda e: self.page.run_task(
                              self.open_url, core.WEB_URL + "/settings/personal-access-tokens/new"))], wrap=True)]
        return ft.Column([ft.Row([ft.Container(self.card(parts, 28), width=620)], alignment=ft.MainAxisAlignment.CENTER)],
                         scroll=ft.ScrollMode.AUTO, expand=True)

    def repo_view(self):
        repo_list = ft.Column(spacing=6, tight=True)
        dev = ft.TextField(label="Nombre de este dispositivo", value=self.ss.device)
        new_name = ft.TextField(label="Nombre del repositorio nuevo", value="mis-partidas", expand=True)

        def choose(full):
            self.run_bg("Preparando {}…".format(full), lambda: self.ss.set_repo(full, dev.value),
                        lambda st: (self.toast("Atención: ese repositorio es PÚBLICO" if st.private is False else "Listo"),
                                    self.route()))

        def create(e):
            name = (new_name.value or "").strip()
            if not name:
                return
            self.run_bg("Creando repositorio privado…", lambda: self.ss.gh.create_repo(name, True),
                        lambda r: choose(r["full_name"]))

        def load():
            return self.ss.gh.repos()

        def show(repos):
            repo_list.controls = [ft.ListTile(
                leading=ft.Icon(ft.Icons.LOCK if r.get("private") else ft.Icons.PUBLIC,
                                color="#2FBF71" if r.get("private") else "#F2B441"),
                title=ft.Text(r["full_name"]),
                subtitle=ft.Text("privado" if r.get("private") else "público: cualquiera puede verlo"),
                on_click=lambda e, f=r["full_name"]: choose(f)) for r in repos] or [
                ft.Text("No hay repositorios visibles con este acceso.", color="#8B93A7")]
            self.page.update()
        self.run_bg(None, load, show, quiet=True)
        return ft.Column([ft.Row([ft.Container(self.card([
            ft.Text("Elige dónde guardar tus partidas", size=22, weight=ft.FontWeight.BOLD),
            ft.Text("Conectado como {}. Usa el mismo repositorio en todos tus dispositivos.".format(
                self.ss.config.get("login")), color="#AAB1C3"),
            dev,
            ft.Text("Crear uno nuevo (recomendado la primera vez)", weight=ft.FontWeight.BOLD),
            ft.Row([new_name, ft.FilledButton("Crear privado", icon=ft.Icons.ADD, on_click=create)]),
            ft.Text("O elige uno existente", weight=ft.FontWeight.BOLD), repo_list,
            ft.TextButton("Cerrar sesión", icon=ft.Icons.LOGOUT,
                          on_click=lambda e: (self.ss.logout(), self.render()))], 24), width=680)],
            alignment=ft.MainAxisAlignment.CENTER)], scroll=ft.ScrollMode.AUTO, expand=True)

    # --- Mis partidas ---------------------------------------------------------
    def filtered_rows(self):
        f = self.filters
        q = f["q"].lower()
        out = []
        for r in self.view["rows"]:
            if r.get("empty") and not f["empty"]:
                continue
            if f["sys"] and r["system"] != f["sys"]:
                continue
            if q and q not in r["name"].lower() and q not in r.get("parent", "").lower():
                continue
            plats = {w["platform"] for w in r["where"]}
            if f["plat"] == "github" and not r["github"]:
                continue
            if f["plat"] not in ("all", "github") and f["plat"] not in plats:
                continue
            out.append(r)
        return sorted(out, key=lambda r: r["name"].lower())

    def set_filter(self, k, v):
        self.filters[k] = v
        self.render()

    def games_view(self):
        if self.view is None:
            return ft.Column([ft.ProgressRing(), ft.Text("Cargando tus partidas…")],
                             horizontal_alignment=ft.CrossAxisAlignment.CENTER, expand=True,
                             alignment=ft.MainAxisAlignment.CENTER)
        v, f = self.view, self.filters
        rows_all = [r for r in v["rows"] if not r.get("empty") or f["empty"]]
        counts = {}
        for r in rows_all:
            for p in {w["platform"] for w in r["where"]}:
                counts[p] = counts.get(p, 0) + 1
        counts.setdefault(self.ss.platform, 0)
        gh_n = sum(1 for r in rows_all if r["github"])

        def chip(key, label, n, color=None):
            return ft.Chip(label=ft.Text("{}  {}".format(label, n)), selected=f["plat"] == key, show_checkmark=False,
                           leading=ft.Icon(ft.Icons.CIRCLE, size=10, color=color) if color else None,
                           on_select=lambda e, k=key: self.set_filter("plat", k), selected_color="#2A2F45")
        chips = [chip("all", "Todas", len(rows_all))]
        chips += [chip(p, plab(p), counts[p], PCOL.get(p)) for p in sorted(counts, key=plab)]
        chips.append(chip("github", "En GitHub", gh_n, PCOL["github"]))
        systems = sorted({r["system"] for r in v["rows"] if r["system"]})
        search = ft.TextField(hint_text="Buscar juego…", value=f["q"], prefix_icon=ft.Icons.SEARCH, dense=True,
                              width=240, on_change=lambda e: self._search(e.control.value))
        sysdd = ft.Dropdown(value=f["sys"] or "", width=240, dense=True,
                            options=[ft.DropdownOption(key="", text="Todos los sistemas")] +
                            [ft.DropdownOption(key=s, text=s) for s in systems],
                            on_select=lambda e: self.set_filter("sys", e.control.value or ""))
        group = ft.SegmentedButton(selected=["group" if f["group"] else "list"], show_selected_icon=False,
                                   segments=[ft.Segment(value="list", label=ft.Text("Todos juntos")),
                                             ft.Segment(value="group", label=ft.Text("Por plataforma"))],
                                   on_change=lambda e: self.set_filter("group", "group" in e.control.selected))
        empty = ft.Checkbox(label="Mostrar sin partidas", value=f["empty"],
                            on_change=lambda e: self.set_filter("empty", e.control.value))
        self.list_area = ft.Column(spacing=0)
        self._fill_list()
        devs = "  ·  ".join("{}{}".format(d["device"], " (este)" if d["this"] else "") for d in v["devices"])
        return ft.Column([
            ft.Row(chips, wrap=True, spacing=8, run_spacing=8),
            ft.Row([search, sysdd, group, empty], wrap=True, spacing=12, run_spacing=8,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Text("{} dispositivos: {}".format(len(v["devices"]), devs), size=12, color="#8B93A7"),
            ft.Container(self.list_area, expand=True),
        ], spacing=12, expand=True, scroll=ft.ScrollMode.AUTO)

    def _search(self, value):
        self.filters["q"] = value or ""
        self._fill_list()
        self.page.update()

    def _fill_list(self):
        rows = self.filtered_rows()
        if not rows:
            self.list_area.controls = [self.card([
                ft.Text("No hay partidas con este filtro.", color="#AAB1C3"),
                ft.Button("Detectar partidas en este dispositivo", icon=ft.Icons.TRAVEL_EXPLORE,
                          on_click=lambda e: self.go(1))])]
            return
        if self.filters["group"]:
            groups = {}
            for r in rows:
                ps = sorted({w["platform"] for w in r["where"]}) + (["github"] if r["github"] else [])
                for p in ps or ["none"]:
                    groups.setdefault(p, []).append(r)
            ctrls = []
            for p in sorted(groups, key=lambda x: (x == "github", x == "none", plab(x))):
                ctrls.append(ft.Container(ft.Row([
                    ft.Icon(ft.Icons.CIRCLE, size=12, color=PCOL.get(p, "#666")),
                    ft.Text("Sin partidas" if p == "none" else plab(p), size=16, weight=ft.FontWeight.BOLD),
                    ft.Text("{} juegos".format(len(groups[p])), color="#8B93A7")]),
                    padding=ft.Padding.only(top=14, bottom=6)))
                ctrls.append(self._list_box(groups[p]))
            self.list_area.controls = ctrls
        else:
            self.list_area.controls = [self._list_box(rows)]

    def _list_box(self, rows):
        return ft.Container(ft.Column([self._row(r) for r in rows], spacing=0), border_radius=14,
                            bgcolor="#151922", border=ft.Border.all(1, "#262C3D"),
                            clip_behavior=ft.ClipBehavior.HARD_EDGE)

    def _row(self, r):
        label, icon, color = STATUS_SHORT.get(r["status"], (r["status"], ft.Icons.HELP, "#888"))
        seen, chips = set(), []
        for w in r["where"]:
            if w["device"] in seen:
                continue
            seen.add(w["device"])
            multi = sum(1 for d in self.view["devices"] if d["platform"] == w["platform"]) > 1
            chips.append(ft.Container(
                ft.Text(plab(w["platform"]) + (" · " + w["device"] if multi else ""), size=11,
                        color=PCOL.get(w["platform"], "#AAA")),
                padding=ft.Padding.symmetric(horizontal=8, vertical=2), border_radius=20,
                border=ft.Border.all(1, PCOL.get(w["platform"], "#666"))))
        if r["github"]:
            chips.append(ft.Container(ft.Row([ft.Icon(ft.Icons.CLOUD_DONE_OUTLINED, size=12, color="#CFD5E2"),
                                               ft.Text(fmt_date(r["github"]["updated_at"]), size=11,
                                                       color="#CFD5E2")], spacing=4, tight=True),
                                      padding=ft.Padding.symmetric(horizontal=8, vertical=2), border_radius=20,
                                      border=ft.Border.all(1, "#3A4258")))
        g0 = self.view["games"].get(r["game_ids"][0], {})
        sub = ("en " + r["parent"]) if r.get("item") else (g0.get("local_path") or "sin carpeta en este dispositivo")
        return ft.Container(
            ft.Row([
                ft.Container(ft.Text(initials(r["name"]), weight=ft.FontWeight.BOLD, color="white"),
                             width=40, height=40, border_radius=10, bgcolor=sys_color(r["system"]),
                             alignment=ft.Alignment.CENTER),
                ft.Column([
                    ft.Row([ft.Text(r["name"], weight=ft.FontWeight.W_600, size=15, max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS, expand=True),
                            ft.Container(ft.Text(r["system"] or "—", size=11), border_radius=6, bgcolor="#1B2030",
                                         padding=ft.Padding.symmetric(horizontal=8, vertical=2))]),
                    ft.Text(sub, size=12, color="#8B93A7", max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Row(chips, wrap=True, spacing=6, run_spacing=4) if chips else ft.Container(),
                ], spacing=4, expand=True, tight=True),
                ft.Row([ft.Icon(icon, size=16, color=color),
                        ft.Text(label, size=12, color=color)], spacing=4, tight=True),
            ], spacing=14, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.symmetric(horizontal=14, vertical=10), ink=True,
            border=ft.Border.only(bottom=ft.BorderSide(1, "#232838")),
            on_click=lambda e, gid=r["game_ids"][0]: self.open_game(gid))

    def go(self, tab):
        self.tab = tab
        self.layout()

    # --- detalle de un juego -----------------------------------------------
    def open_game(self, gid):
        g = self.view["games"][gid]
        label, icon, color = STATUS_SHORT.get(g["status"], (g["status"], ft.Icons.HELP, "#888"))
        path_field = ft.TextField(label="Carpeta en este dispositivo", value=g.get("local_path") or "", expand=True)
        shared = ft.Checkbox(label="Usar esta carpeta en todos mis dispositivos {}".format(plab(self.ss.platform)),
                             value=not g.get("override"))

        def kv(k, v):
            return ft.Row([ft.Text(k, color="#8B93A7", width=130), ft.Text(v, selectable=True, expand=True)],
                          vertical_alignment=ft.CrossAxisAlignment.START)
        items = [r["name"] for r in self.view["rows"] if r.get("item") and gid in r["game_ids"]]
        ctrls = [
            kv("Estado aquí", STATUS_LONG.get(g["status"], g["status"])),
            kv("Sistema", (g.get("system") or "—") + (" · carpeta de emulador" if g.get("type") == "collection" else "")),
            kv("En GitHub", "{} desde {} ({} archivos)".format(fmt_date(g["github"]["updated_at"]), g["github"]["device"],
                                                              g["github"]["files"]) if g["github"] else "todavía no se subió"),
            kv("Archivos", ", ".join(g.get("include", ["*"])) + (" · excluye " + ", ".join(g["exclude"])
                                                                if g.get("exclude") else "")),
        ]
        if g.get("notes"):
            ctrls.append(kv("Notas", g["notes"]))
        ctrls.append(ft.Text("Carpetas por plataforma", weight=ft.FontWeight.BOLD))
        for p, val in (g.get("paths") or {}).items():
            ctrls.append(ft.Row([ft.Icon(ft.Icons.CIRCLE, size=10, color=PCOL.get(p, "#666")),
                                 ft.Text(plab(p), width=100), ft.Text(val, size=12, selectable=True, expand=True,
                                                                      font_family="monospace")]))
        ctrls.append(ft.Text("Dispositivos con partida", weight=ft.FontWeight.BOLD))
        for w in g["where"] or []:
            ctrls.append(ft.Row([ft.Icon(ft.Icons.CIRCLE, size=10, color=PCOL.get(w["platform"], "#666")),
                                 ft.Text("{}{}".format(w["device"], " (este)" if w["this"] else ""), expand=True),
                                 ft.Text("{} archivos · {} · {}".format(w.get("files"), core.human_bytes(w.get("bytes")),
                                                                        fmt_date(w.get("modified"))),
                                         size=12, color="#8B93A7")]))
        if not g["where"]:
            ctrls.append(ft.Text("ninguno", color="#8B93A7"))
        if items:
            ctrls.append(ft.Text("Juegos dentro ({})".format(len(items)), weight=ft.FontWeight.BOLD))
            ctrls.append(ft.Row([ft.Container(ft.Text(i, size=11), bgcolor="#1B2030", border_radius=6,
                                              padding=ft.Padding.symmetric(horizontal=8, vertical=3))
                                 for i in items[:80]], wrap=True, spacing=6, run_spacing=6))
        ctrls += [ft.Text("Cambiar carpeta", weight=ft.FontWeight.BOLD),
                  ft.Row([path_field, ft.IconButton(ft.Icons.FOLDER_OPEN, tooltip="Explorar",
                                                    on_click=lambda e: self.browse(path_field, reopen=holder["dlg"]))]),
                  shared,
                  ft.Button("Guardar carpeta", icon=ft.Icons.SAVE, on_click=lambda e: save_path())]
        if self.ss.platform in ("steamdeck", "linux", "windows"):
            if self.ss.platform == "windows":
                exe = '"{}"'.format(os.path.join(os.environ.get("LOCALAPPDATA", ""), "SaveSync", "savesync-cli.exe"))
            else:
                exe = str(Path.home() / "savesync" / "savesync-cli")
            launch = '{} run {} -- %command%'.format(exe, gid)
            ctrls += [ft.Text("Sincronizar al jugar (Steam)", weight=ft.FontWeight.BOLD),
                      ft.Text("Pega esto en Steam > juego > Propiedades > Opciones de lanzamiento:", size=12,
                              color="#8B93A7"),
                      ft.Row([ft.Text(launch, size=12, font_family="monospace", selectable=True, expand=True),
                              ft.IconButton(ft.Icons.COPY, on_click=lambda e: self.page.run_task(self.copy, launch))])]

        holder = {}

        def save_path():
            raw = (path_field.value or "").strip()
            if not raw:
                return
            path = raw if any(c in raw for c in "%~$") else core.portable_path(raw, self.ss.platform)
            self.close_dialog()
            self.run_bg("Guardando carpeta…", lambda: self.ss.set_path(gid, path, shared.value),
                        lambda _: (self.toast("Carpeta guardada"), self.refresh(False)))
        holder["dlg"] = self.dialog(g["name"], ft.Container(ft.Column(ctrls, spacing=10, tight=True), width=640), [
            ft.TextButton("Quitar", icon=ft.Icons.DELETE_OUTLINE, on_click=lambda e: self.confirm_remove(gid)),
            ft.TextButton("Historial", icon=ft.Icons.HISTORY, on_click=lambda e: self.history(gid)),
            ft.TextButton("Bajar de GitHub", icon=ft.Icons.DOWNLOAD, disabled=not g["github"] or not g.get("local_path"),
                          on_click=lambda e: self.sync([gid], "pull")),
            ft.TextButton("Subir esta", icon=ft.Icons.UPLOAD, disabled=g["status"] in ("sin-ruta", "vacio", "solo-remoto"),
                          on_click=lambda e: self.sync([gid], "push")),
            ft.FilledButton("Sincronizar", icon=ft.Icons.SYNC, on_click=lambda e: self.sync([gid])),
        ])

    def confirm_remove(self, gid):
        g = self.view["games"][gid]
        delete = ft.Checkbox(label="Borrar también sus partidas del repositorio (quedan en el historial)")
        self.close_dialog()

        def do(e):
            self.close_dialog()
            self.run_bg("Quitando…", lambda: self.ss.remove_game(gid, delete.value),
                        lambda _: (self.toast("Quitado"), self.refresh(False)))
        self.dialog("Quitar " + g["name"], ft.Column([
            ft.Text("Se quitará de la lista en todos tus dispositivos. Tus partidas locales no se borran."),
            delete], tight=True), [ft.TextButton("Cancelar", on_click=self.close_dialog),
                                   ft.FilledButton("Quitar", on_click=do)])

    def history(self, gid):
        g = self.view["games"][gid]
        self.close_dialog()

        def show(commits):
            def restore(sha):
                self.close_dialog()
                self.run_bg("Restaurando…", lambda: self.ss.restore(gid, sha),
                            lambda _: (self.toast("Versión restaurada. Usa «Subir esta» si quieres que sea la de GitHub."),
                                       self.refresh(False)))
            rows = [ft.ListTile(title=ft.Text(c["message"].splitlines()[0], size=13),
                                subtitle=ft.Text("{} · {}".format(c["short"], fmt_date(c["date"])), size=12),
                                trailing=ft.TextButton("Restaurar", on_click=lambda e, s=c["sha"]: restore(s)))
                    for c in commits] or [ft.Text("Todavía no hay versiones en GitHub.")]
            bks = self.ss.list_backups(gid)
            if bks:
                rows.append(ft.Text("Copias de seguridad en este dispositivo", weight=ft.FontWeight.BOLD))
                rows += [ft.ListTile(title=ft.Text(b), trailing=ft.TextButton(
                    "Restaurar", on_click=lambda e, b=b: (self.close_dialog(), self.run_bg(
                        "Restaurando…", lambda: self.ss.restore_backup(gid, b),
                        lambda _: (self.toast("Copia restaurada"), self.refresh(False)))))) for b in bks]
            self.dialog("Historial · " + g["name"], ft.Container(ft.Column(rows, tight=True, spacing=0), width=600),
                        [ft.TextButton("Cerrar", on_click=self.close_dialog)])
        self.run_bg("Leyendo historial…", lambda: self.ss.history(gid), show)

    # --- sincronizar -----------------------------------------------------------
    def sync(self, ids, mode="auto", prefer=None):
        self.close_dialog()

        def work():
            return self.ss.sync(ids, mode=mode, prefer=prefer)

        def done(results):
            conflicts = [r for r in results if r[2] == "conflict"]
            ups = sum(1 for r in results if r[2] == "up")
            downs = sum(1 for r in results if r[2] == "down")
            if conflicts:
                self.show_conflicts(conflicts)
            elif ups or downs:
                self.toast("Listo: {} subidas, {} descargadas".format(ups, downs))
            else:
                self.toast("Todo estaba al día")
            self.refresh(False)
        label = {"pull": "Descargando de GitHub…", "push": "Subiendo a GitHub…"}.get(mode, "Sincronizando…")
        self.run_bg(label, work, done)

    def show_conflicts(self, conflicts):
        rows = []
        for gid, name, _ in conflicts:
            rows.append(ft.Text(name, weight=ft.FontWeight.BOLD))
            rows.append(ft.Row([ft.Button("Usar la de aquí", icon=ft.Icons.UPLOAD,
                                          on_click=lambda e, g=gid: self.sync([g], "push")),
                                ft.Button("Usar la de GitHub", icon=ft.Icons.DOWNLOAD,
                                          on_click=lambda e, g=gid: self.sync([g], "pull"))], wrap=True))
        self.dialog("Conflictos", ft.Column(
            [ft.Text("Estos juegos cambiaron en este dispositivo y en otro. Elige qué versión conservar; "
                     "la otra queda en el historial o en la copia de seguridad.", color="#AAB1C3")] + rows,
            tight=True, spacing=10), [ft.TextButton("Más tarde", on_click=self.close_dialog)])

    # --- explorador de carpetas -------------------------------------------
    def shortcuts(self):
        home = Path.home()
        sc = []
        if self.ss.platform == "windows":
            sc.append(("Inicio", str(home)))
            for var, lab in (("APPDATA", "AppData\\Roaming"), ("LOCALAPPDATA", "AppData\\Local")):
                if os.environ.get(var):
                    sc.append((lab, os.environ[var]))
            sc += [("AppData\\LocalLow", str(home / "AppData" / "LocalLow")),
                   ("Documentos", str(det.documents_dir())), ("Saved Games", str(home / "Saved Games"))]
            sc += [(d + ":", d + ":\\") for d in string.ascii_uppercase if os.path.exists(d + ":\\")]
        elif self.ss.platform == "android":
            sc.append(("Almacenamiento", det.SD))
            for sub in ("RetroArch", "Download", "Documents"):
                if os.path.isdir(os.path.join(det.SD, sub)):
                    sc.append((sub, os.path.join(det.SD, sub)))
        else:
            sc.append(("Inicio", str(home)))
            for p, lab in ((home / "Emulation", "EmuDeck"), (home / ".config", ".config"),
                           (home / ".local/share", ".local/share")):
                if p.is_dir():
                    sc.append((lab, str(p)))
            for lib in core.steam_libraries(self.ss.platform):
                if (lib / "steamapps/compatdata").is_dir():
                    sc.append(("Proton", str(lib / "steamapps/compatdata")))
        return [s for s in sc if os.path.isdir(s[1])]

    def browse(self, target_field, start=None, reopen=None):
        """Explorador propio (funciona igual en escritorio, Steam Deck y Android).
        reopen: diálogo a volver a mostrar al terminar (si se abrió desde otro diálogo)."""
        start = start or core.expand_path(target_field.value or "") or Path.home()
        fallback = Path(det.SD) if self.ss.platform == "android" else Path.home()
        cur = {"path": Path(start) if Path(start).is_dir() else fallback}
        listing = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, height=360)
        path_text = ft.Text(size=12, font_family="monospace", selectable=True)

        def fill():
            p = cur["path"]
            path_text.value = str(p)
            entries = []
            if p.parent != p:
                entries.append(ft.ListTile(leading=ft.Icon(ft.Icons.ARROW_UPWARD), title=ft.Text(".."), dense=True,
                                           on_click=lambda e: nav(p.parent)))
            try:
                items = sorted(os.scandir(p), key=lambda e: (not e.is_dir(), e.name.lower()))
            except OSError as ex:
                items = []
                entries.append(ft.Text("No se puede abrir: {}".format(ex), color="#F2B441"))
            for it in items[:400]:
                try:
                    is_dir = it.is_dir()
                except OSError:
                    continue
                entries.append(ft.ListTile(
                    leading=ft.Icon(ft.Icons.FOLDER if is_dir else ft.Icons.INSERT_DRIVE_FILE_OUTLINED,
                                    color="#F5C451" if is_dir else "#6B7280"),
                    title=ft.Text(it.name, size=13, color=None if is_dir else "#8B93A7"), dense=True,
                    on_click=(lambda e, n=it.name: nav(p / n)) if is_dir else None))
            listing.controls = entries
            self.page.update()

        def nav(p):
            cur["path"] = Path(p)
            fill()

        def finish(value):
            self.close_dialog()
            if value is not None:
                target_field.value = value
            if reopen is not None:
                self.page.show_dialog(reopen)
            self.page.update()
        sc_row = ft.Row([ft.OutlinedButton(lab, on_click=lambda e, p=p: nav(p)) for lab, p in self.shortcuts()],
                        wrap=True, spacing=6, run_spacing=6)
        if reopen is not None:
            self.close_dialog()
        self.dialog("Elegir carpeta", ft.Container(ft.Column([sc_row, path_text, listing], tight=True), width=620),
                    [ft.TextButton("Cancelar", on_click=lambda e: finish(None)),
                     ft.FilledButton("Usar esta carpeta", icon=ft.Icons.CHECK,
                                     on_click=lambda e: finish(str(cur["path"])))], scrollable=False)
        fill()

    # --- Detectar ----------------------------------------------------------------
    def detect_view(self):
        db = det.db_info(self.ss)
        if self.ss.platform == "android":
            db_txt = "En Android se buscan carpetas de emuladores."
        elif db.get("exists"):
            db_txt = "Base de datos de juegos de PC: {:,} juegos · {}".format(db["games"], db["updated"]).replace(",", ".")
        else:
            db_txt = "La primera búsqueda descarga la base de datos de Ludusavi (~18 MB)."
        top = [ft.Text("Detectar partidas en {}".format(self.ss.device), size=20, weight=ft.FontWeight.BOLD),
               ft.Text("Busca donde normalmente se guardan las partidas: juegos de PC (también dentro de Proton), "
                       "RetroArch, PCSX2, DuckStation, PPSSPP, Dolphin, Ryujinx, yuzu y derivados, Citra/Azahar, "
                       "Cemu, RPCS3 y EmuDeck.", color="#AAB1C3"),
               ft.Row([ft.FilledButton("Buscar partidas", icon=ft.Icons.SEARCH, on_click=lambda e: self.scan()),
                       ft.TextButton("Actualizar base de datos", icon=ft.Icons.DOWNLOAD,
                                     on_click=lambda e: self.run_bg("Descargando base de datos…",
                                                                    lambda: det.update_db(self.ss, self.log),
                                                                    lambda n: self.render()),
                                     visible=self.ss.platform != "android"),
                       ft.Text(db_txt, size=12, color="#8B93A7")], wrap=True)]
        if not self.perm_ok:
            top.append(self.permission_banner())
        ctrls = [self.card(top)]
        if self.scan_results is not None:
            res = self.scan_results
            if not res:
                ctrls.append(self.card([ft.Text("No se encontraron partidas en las ubicaciones conocidas. "
                                                "Puedes agregarlas en «Agregar».")]))
            else:
                lab = {"new": ("Nuevo", "#2FBF71"), "merge": ("Añadir carpeta", "#5B8CFF"),
                       "known": ("Ya agregado", "#8B93A7")}
                sync_after = ft.Switch(label="Subir a GitHub después", value=True)
                rows = []
                for i, c in enumerate(res):
                    t, col = lab[c["status"]]
                    rows.append(ft.Container(ft.Row([
                        ft.Checkbox(value=i in self.scan_sel, disabled=c["status"] == "known",
                                    on_change=lambda e, i=i: self._toggle(i, e.control.value)),
                        ft.Column([
                            ft.Row([ft.Text(c["name"], weight=ft.FontWeight.W_600),
                                    ft.Container(ft.Text(c["system"], size=11), bgcolor="#1B2030", border_radius=6,
                                                 padding=ft.Padding.symmetric(horizontal=8, vertical=2))], wrap=True),
                            ft.Text(c["path"], size=11, color="#8B93A7", font_family="monospace", selectable=True),
                            ft.Text(" · ".join(c.get("items", [])[:6]), size=11, color="#8B93A7",
                                    visible=bool(c.get("items"))),
                        ], spacing=2, expand=True, tight=True),
                        ft.Column([ft.Text("{} archivos".format(c["files"]), size=12),
                                   ft.Text(core.human_bytes(c["bytes"]), size=11, color="#8B93A7")],
                                  horizontal_alignment=ft.CrossAxisAlignment.END, tight=True, spacing=2),
                        ft.Container(ft.Text(t, size=12, color=col), width=110),
                    ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        padding=ft.Padding.symmetric(horizontal=8, vertical=8),
                        border=ft.Border.only(bottom=ft.BorderSide(1, "#232838"))))
                ctrls.append(self.card([
                    ft.Row([ft.Text("{} encontrados".format(len(res)), weight=ft.FontWeight.BOLD),
                            ft.Container(expand=True), sync_after,
                            ft.FilledButton("Agregar seleccionados ({})".format(len(self.scan_sel)), icon=ft.Icons.ADD,
                                            disabled=not self.scan_sel,
                                            on_click=lambda e: self.add_detected(sync_after.value))]),
                    ft.Column(rows, spacing=0)], 14))
        return ft.Column(ctrls, spacing=14, scroll=ft.ScrollMode.AUTO, expand=True,
                         horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    def _toggle(self, i, on):
        (self.scan_sel.add if on else self.scan_sel.discard)(i)
        self.render()

    def scan(self):
        def work():
            with self.ss.lock:
                self.ss.store.snapshot(refresh=True)
                return det.scan(self.ss, progress=self.log)

        def done(res):
            self.scan_results = res
            self.scan_sel = {i for i, c in enumerate(res) if c["status"] != "known"}
            self.render()
        self.run_bg("Buscando partidas… (la primera vez puede tardar)", work, done)

    def add_detected(self, sync_after):
        cands = [self.scan_results[i] for i in sorted(self.scan_sel)]

        def work():
            res = self.ss.add_many([(det.candidate_entry(c), c["path_store"]) for c in cands])
            if sync_after:
                self.ss.sync([r[0] for r in res])
            return res

        def done(res):
            self.toast("{} juegos agregados".format(len(res)))
            self.scan_results, self.scan_sel = None, set()
            self.tab = 0
            self.layout()
            self.refresh(False)
        self.run_bg("Agregando y subiendo…" if sync_after else "Agregando…", work, done)

    # --- Agregar manual ------------------------------------------------------------
    def add_view(self):
        f = self.add_form
        name = ft.TextField(label="Nombre", value=f.get("name", ""), hint_text="Ej: Hollow Knight")
        system = ft.Dropdown(label="Sistema", value=f.get("system", "PC"), editable=True, enable_filter=True,
                             options=[ft.DropdownOption(key=s, text=s) for s in SYSTEMS])
        kind = ft.SegmentedButton(selected=[f.get("type", "game")], segments=[
            ft.Segment(value="game", label=ft.Text("Un juego")),
            ft.Segment(value="collection", label=ft.Text("Carpeta de emulador (varios juegos)"))])
        path = ft.TextField(label="Carpeta de partidas", value=f.get("path", ""), expand=True,
                            hint_text="Ej: %APPDATA%\\MiJuego  ·  ~/.local/share/MiJuego")
        include = ft.TextField(label="Incluir archivos (separados por coma)", value=f.get("include", "*"))
        exclude = ft.TextField(label="Excluir (opcional)", value=f.get("exclude", ""), hint_text="Ej: *.log, cache/*")
        shared = ft.Checkbox(label="Usar esta carpeta en todos mis dispositivos {}".format(plab(self.ss.platform)),
                             value=f.get("shared", True))
        sync_after = ft.Checkbox(label="Subir la partida a GitHub al agregar", value=f.get("sync", True))
        preview = ft.Column(tight=True)

        def split(s):
            return [x.strip() for x in (s or "").split(",") if x.strip()]

        def read():
            f.update(name=name.value, system=system.value or "PC", type=(kind.selected or ["game"])[0],
                     path=path.value, include=include.value, exclude=exclude.value, shared=shared.value,
                     sync=sync_after.value)

        def do_preview(e=None):
            read()
            p = core.expand_path(f["path"])
            if not p or not p.is_dir():
                preview.controls = [ft.Text("La carpeta no existe en este dispositivo.", color="#F2B441")]
            else:
                files = core.collect_files(p, split(f["include"]) or ["*"], split(f["exclude"]))
                size = sum(x.stat().st_size for x in files.values())
                preview.controls = [ft.Text("{} archivos · {}".format(len(files), core.human_bytes(size)),
                                            weight=ft.FontWeight.BOLD)]
                preview.controls += [ft.Text(r, size=11, font_family="monospace", color="#8B93A7")
                                     for r in sorted(files)[:12]]
                if f["type"] == "collection":
                    its = [i["name"] for i in core.collection_items(files.keys(), {"system": f["system"]})][:20]
                    preview.controls.append(ft.Text("Juegos: " + " · ".join(its), size=12))
            self.page.update()

        def do_add(e):
            read()
            if not f["name"].strip() or not f["path"].strip():
                return self.toast("Completa el nombre y la carpeta.", True)
            raw = f["path"].strip().strip('"')
            store_path = raw if any(c in raw for c in "%~$") else core.portable_path(raw, self.ss.platform)
            entry = {"id": core.slugify(f["name"]), "name": f["name"].strip(), "system": f["system"],
                     "include": split(f["include"]) or ["*"]}
            if split(f["exclude"]):
                entry["exclude"] = split(f["exclude"])
            if f["type"] == "collection":
                entry["type"] = "collection"

            def work():
                g, created = self.ss.add_or_merge(entry, store_path, f["shared"])
                if f["sync"]:
                    self.ss.sync([g["id"]])
                return g

            def done(g):
                self.add_form = {}
                self.toast("Agregado: " + g["name"])
                self.tab = 0
                self.layout()
                self.refresh(False)
            self.run_bg("Agregando…", work, done)
        return ft.Column([self.card([
            ft.Text("Agregar un juego a mano", size=20, weight=ft.FontWeight.BOLD),
            ft.Text("Para juegos o emuladores que la detección no encuentra. En los demás dispositivos podrás "
                    "indicar su carpeta desde «Partidas».", color="#AAB1C3"),
            name, system, kind,
            ft.Row([path, ft.IconButton(ft.Icons.FOLDER_OPEN, tooltip="Explorar", on_click=lambda e: self.browse(path))]),
            include, exclude, shared, sync_after,
            ft.Row([ft.OutlinedButton("Vista previa", icon=ft.Icons.VISIBILITY, on_click=do_preview),
                    ft.FilledButton("Agregar juego", icon=ft.Icons.ADD, on_click=do_add)]),
            preview], 22)], scroll=ft.ScrollMode.AUTO, expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    # --- Ajustes ----------------------------------------------------------------------
    def settings_view(self):
        dev = ft.TextField(label="Nombre de este dispositivo", value=self.ss.device, expand=True)
        keep = ft.TextField(label="Copias de seguridad por juego", value=str(self.ss.config.get("keep_backups", 10)),
                            width=220, keyboard_type=ft.KeyboardType.NUMBER)

        def save_dev(e):
            self.run_bg("Guardando…", lambda: self.ss.set_device_name(dev.value),
                        lambda _: (self.toast("Guardado"), self.layout(), self.refresh(False)))

        def save_keep(e):
            try:
                self.ss.config["keep_backups"] = max(1, int(keep.value))
                self.ss.save_config()
                self.toast("Guardado")
            except ValueError:
                self.toast("Escribe un número.", True)

        def change_repo(e):
            self.ss.config.pop("repo", None)
            self.ss.save_config()
            self.ss.reload()
            self.view = None
            self.layout()

        def logout(e):
            self.ss.logout()
            self.view = None
            self.layout()
        devices = (self.view or {}).get("devices", [])
        logs = ft.Column([ft.Text(l, size=11, font_family="monospace", selectable=True) for l in self.logs[-80:]],
                         spacing=0, height=200, scroll=ft.ScrollMode.AUTO, auto_scroll=True)
        ctrls = [
            self.card([ft.Text("Este dispositivo", size=18, weight=ft.FontWeight.BOLD),
                       ft.Row([dev, ft.Button("Guardar", on_click=save_dev)]),
                       ft.Text("Plataforma: {}".format(plab(self.ss.platform)), color="#AAB1C3"),
                       ft.Row([keep, ft.Button("Guardar", on_click=save_keep)]),
                       ft.Text("Datos de SaveSync: {}".format(self.ss.home), size=12, color="#8B93A7", selectable=True)]),
            self.card([ft.Text("GitHub", size=18, weight=ft.FontWeight.BOLD),
                       ft.Text("Cuenta: {}".format(self.ss.config.get("login")), color="#AAB1C3"),
                       ft.Text("Repositorio de partidas: {}".format(self.ss.config.get("repo")), color="#AAB1C3"),
                       ft.Row([ft.OutlinedButton("Abrir en GitHub", icon=ft.Icons.OPEN_IN_NEW,
                                                 on_click=lambda e: self.page.run_task(
                                                     self.open_url, "{}/{}".format(core.WEB_URL, self.ss.config.get("repo")))),
                               ft.OutlinedButton("Cambiar repositorio", icon=ft.Icons.SWAP_HORIZ, on_click=change_repo),
                               ft.OutlinedButton("Cerrar sesión", icon=ft.Icons.LOGOUT, on_click=logout)], wrap=True)]),
            self.card([ft.Text("Dispositivos", size=18, weight=ft.FontWeight.BOLD)] + [
                ft.Row([ft.Icon(ft.Icons.CIRCLE, size=10, color=PCOL.get(d["platform"], "#666")),
                        ft.Text("{}{}".format(d["device"], " (este)" if d["this"] else ""), expand=True),
                        ft.Text("{} · {} juegos · {}".format(plab(d["platform"]), d["games"], fmt_date(d["updated_at"])),
                                size=12, color="#8B93A7")]) for d in devices]),
            self.card([ft.Text("Registro", size=18, weight=ft.FontWeight.BOLD), logs]),
            ft.Text("SaveSync {} · Base de datos de juegos de PC: Ludusavi / PCGamingWiki".format(core.VERSION),
                    size=12, color="#8B93A7"),
        ]
        if core.is_android():
            ctrls.insert(1, self.permission_banner() if not self.perm_ok else self.card(
                [ft.Text("Acceso a archivos: permitido", color="#2FBF71")]))
        return ft.Column(ctrls, spacing=14, scroll=ft.ScrollMode.AUTO, expand=True,
                         horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    # --- Android -------------------------------------------------------------------
    def permission_banner(self):
        return self.card([
            ft.Text("Permiso de archivos", size=16, weight=ft.FontWeight.BOLD),
            ft.Text("Para leer y escribir las partidas de tus emuladores, SaveSync necesita «Acceso a todos los "
                    "archivos». Android no permite acceder a la carpeta Android/data: configura tus emuladores para "
                    "guardar en una carpeta normal (ej. /storage/emulated/0/RetroArch/saves).", color="#AAB1C3"),
            ft.FilledButton("Permitir acceso", icon=ft.Icons.FOLDER_SHARED,
                            on_click=lambda e: self.page.run_task(self.request_android_permission))])

    async def check_android_permission(self):
        try:
            import flet_permission_handler as fph
            ph = fph.PermissionHandler()
            status = await ph.get_status(fph.Permission.MANAGE_EXTERNAL_STORAGE)
            self.perm_ok = status == fph.PermissionStatus.GRANTED
        except Exception as ex:
            self.log("permiso: {}".format(ex))
            self.perm_ok = os.access(det.SD, os.R_OK | os.W_OK)
        if not self.perm_ok:
            self.render()

    async def request_android_permission(self):
        try:
            import flet_permission_handler as fph
            ph = fph.PermissionHandler()
            status = await ph.request(fph.Permission.MANAGE_EXTERNAL_STORAGE)
            self.perm_ok = status == fph.PermissionStatus.GRANTED
        except Exception as ex:
            self.toast("No se pudo pedir el permiso: {}".format(ex), True)
        self.render()


def main(page: ft.Page):
    App(page)
