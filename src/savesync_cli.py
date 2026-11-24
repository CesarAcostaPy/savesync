#!/usr/bin/env python3
"""SaveSync - línea de comandos.

    savesync login                 conectar tu cuenta de GitHub
    savesync repo                  elegir / crear el repositorio de partidas
    savesync sync                  sincronizar todo
    savesync run <juego> -- <cmd>  sincronizar, abrir el juego y sincronizar al cerrar (Steam)
"""
import argparse
import getpass
import subprocess
import sys
import time
import webbrowser

import savesync_core as core


def info(msg=""):
    print(msg, flush=True)


def ask_conflict(game, a):
    m = a["manifest"] or {}
    info("- {}".format(game["name"]))
    info("   CONFLICTO: cambió aquí y también en '{}' ({})".format(m.get("device", "?"), m.get("updated_at", "?")))
    if not sys.stdin.isatty():
        return None
    while True:
        ans = input("   ¿Qué versión conservar? [l]ocal / [r]emota (GitHub) / [s]altar: ").strip().lower()
        if ans in ("l", "local"):
            return "local"
        if ans in ("r", "remota", "remote"):
            return "remote"
        if ans in ("s", "saltar", ""):
            return None


# ---------------------------------------------------------------------------
def cmd_login(ss, args):
    if args.token:
        user = ss.login_with_token(args.token)
    elif core.OAUTH_CLIENT_ID and not args.ask_token:
        flow = core.device_flow_start()
        info("1. Abre {}".format(flow["verification_uri"]))
        info("2. Escribe este código:  {}".format(flow["user_code"]))
        try:
            webbrowser.open(flow["verification_uri"])
        except Exception:
            pass
        interval = int(flow.get("interval", 5))
        deadline = time.time() + int(flow.get("expires_in", 900))
        while time.time() < deadline:
            time.sleep(interval)
            st, token = core.device_flow_poll(flow["device_code"])
            if st == "ok":
                user = ss.login_with_token(token, "oauth")
                break
            if st == "slow_down":
                interval += 5
            elif st in ("expired", "denied"):
                raise core.SaveSyncError("Inicio de sesión cancelado o expirado.")
        else:
            raise core.SaveSyncError("El código expiró. Vuelve a intentarlo.")
    else:
        info("Crea un token en GitHub: Settings > Developer settings > Personal access tokens >")
        info("Fine-grained tokens, con acceso a tu repositorio de partidas y permiso 'Contents: Read and write'.")
        token = getpass.getpass("Pega el token (no se muestra): ")
        user = ss.login_with_token(token)
    info("Conectado como {}.".format(user.get("login")))
    if not ss.config.get("repo"):
        info("Siguiente paso: savesync repo")


def cmd_logout(ss, args):
    ss.logout()
    info("Sesión cerrada en este dispositivo.")


def cmd_repo(ss, args):
    if args.create:
        r = ss.gh.create_repo(args.create, private=True)
        name = r["full_name"]
        info("Creado el repositorio privado {}.".format(name))
    elif args.name:
        name = args.name if "/" in args.name else "{}/{}".format(ss.config.get("login"), args.name)
    else:
        repos = ss.gh.repos()
        for i, r in enumerate(repos, 1):
            info("{:>3}. {}{}".format(i, r["full_name"], "  (privado)" if r.get("private") else "  (PÚBLICO)"))
        sel = input("Número del repositorio de partidas (o nombre nuevo para crearlo privado): ").strip()
        if sel.isdigit():
            name = repos[int(sel) - 1]["full_name"]
        else:
            name = ss.gh.create_repo(sel, private=True)["full_name"]
            info("Creado {} (privado).".format(name))
    store = ss.set_repo(name, args.device)
    if store.private is False:
        core_warn = "⚠ El repositorio {} es PÚBLICO: cualquiera podría ver tus partidas.".format(name)
        info(core_warn)
    info("Listo. Repositorio: {}  ·  Dispositivo: {}".format(name, ss.device))


def cmd_info(ss, args):
    info("SaveSync {}".format(core.VERSION))
    info("Plataforma : {}".format(core.PLATFORM_LABELS.get(ss.platform, ss.platform)))
    info("Dispositivo: {}".format(ss.device))
    info("Cuenta     : {}".format(ss.config.get("login") or "(sin conectar)"))
    info("Repositorio: {}".format(ss.config.get("repo") or "(sin elegir)"))
    info("Datos      : {}".format(ss.home))
    up = core.update_available()
    if up:
        info("\nHay una versión nueva: {}  ->  {}".format(up["version"], up["url"]))


def cmd_list(ss, args):
    ss.store.snapshot(refresh=True)
    for g in ss.load_games():
        a = ss.analyze(g)
        m = a["manifest"] or {}
        info("[{}] {}  ({})".format(g["id"], g["name"], g.get("system", "")))
        info("   carpeta aquí: {}".format(a["path"] or "(sin carpeta en esta plataforma; usa set-path)"))
        if a["path"]:
            info("   archivos    : {}".format(len(a["local_files"])))
        if m:
            info("   en GitHub   : subido por '{}' el {}".format(m.get("device"), (m.get("updated_at") or "")[:16]))
        info("   estado      : {}".format(core.STATUS_TEXT.get(a["status"], a["status"])))
        info()


def cmd_status(ss, args):
    ss.store.snapshot(refresh=True)
    games = ss.load_games()
    for g in ([ss.get_game(games, i) for i in args.games] if args.games else games):
        info("{:<22} {}".format(g["id"], core.STATUS_TEXT.get(ss.analyze(g, False)["status"])))


def cmd_sync(ss, args, mode="auto"):
    ss.sync(args.games, mode=mode, prefer=getattr(args, "prefer", None), ask=ask_conflict, dry_run=args.dry_run)


def cmd_run(ss, args):
    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise core.SaveSyncError("Falta el comando del juego. Ejemplo: savesync run elden-ring -- %command%")
    info("== SaveSync: antes de jugar ==")
    try:
        ss.sync([args.id])
    except core.SaveSyncError as e:
        info("⚠ No se pudo sincronizar antes de jugar ({}). Se juega igual.".format(e))
    code = subprocess.call(cmd, env=core.child_env())
    info("== SaveSync: después de jugar ==")
    try:
        ss.sync([args.id])
    except core.SaveSyncError as e:
        info("⚠ No se pudo subir la partida ({}). Se subirá la próxima vez.".format(e))
    return code


def cmd_add(ss, args):
    entry = {"id": core.slugify(args.id), "name": args.name or args.id, "system": args.system,
             "include": args.include or ["*"]}
    if args.exclude:
        entry["exclude"] = args.exclude
    if args.collection:
        entry["type"] = "collection"
    path = args.path if any(c in args.path for c in "%~$") else core.portable_path(args.path, ss.platform)
    g, created = ss.add_or_merge(entry, path)
    info("{} '{}' con carpeta {}.".format("Añadido" if created else "Actualizado", g["name"], path))


def cmd_set_path(ss, args):
    path = args.path if any(c in args.path for c in "%~$") else core.portable_path(args.path, ss.platform)
    ss.set_path(args.id, path, shared=not args.only_here)
    info("Carpeta guardada: {}".format(path))


def cmd_scan(ss, args):
    import savesync_detect as det
    ss.store.snapshot(refresh=True)
    cands = det.scan(ss, progress=info)
    for c in cands:
        mark = {"new": "NUEVO", "merge": "AÑADIR RUTA", "known": "ya agregado"}[c["status"]]
        info("[{:<11}] {} ({})  {} archivos  {}".format(mark, c["name"], c["system"], c["files"], c["path"]))
    todo = [c for c in cands if c["status"] != "known"]
    if args.add and todo:
        res = ss.add_many([(det.candidate_entry(c), c["path_store"]) for c in todo])
        info("\nAñadidos/actualizados {} juegos. Ejecuta 'savesync sync' para subir sus partidas.".format(len(res)))


def cmd_remove(ss, args):
    g = ss.remove_game(args.id, args.delete_remote)
    info("Quitado '{}'.".format(g["name"]))


def cmd_history(ss, args):
    for c in ss.history(args.id, args.n):
        info("{}  {}  {}".format(c["short"], c["date"][:16].replace("T", " "), c["message"].splitlines()[0]))
    info("\nPara recuperar una versión: savesync restore {} <versión>".format(args.id))


def cmd_restore(ss, args):
    ss.store.snapshot(refresh=True)
    sha = args.commit
    if len(sha) < 40:
        full = [c["sha"] for c in ss.history(args.id, 100) if c["sha"].startswith(sha)]
        if not full:
            raise core.SaveSyncError("No encuentro la versión {}.".format(sha))
        sha = full[0]
    ss.restore(args.id, sha)
    info("Usa 'savesync push {}' si quieres que esta sea la versión en GitHub.".format(args.id))


def cmd_backups(ss, args):
    ss.store.snapshot(refresh=True)
    for g in ss.load_games():
        b = ss.list_backups(g["id"])
        if b or args.all:
            info("[{}] {}".format(g["id"], ", ".join(b) or "(sin copias)"))
    info("\nCarpeta: {}".format(ss.backup_root))


def build_parser():
    p = argparse.ArgumentParser(prog="savesync", description="Sincroniza tus partidas guardadas con GitHub.")
    p.add_argument("--version", action="version", version="SaveSync " + core.VERSION)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("login", help="conectar tu cuenta de GitHub")
    s.add_argument("--token", help="usar un token personal")
    s.add_argument("--ask-token", action="store_true", help="pedir un token en vez del código")
    sub.add_parser("logout", help="cerrar sesión en este dispositivo")
    s = sub.add_parser("repo", help="elegir o crear el repositorio de partidas")
    s.add_argument("name", nargs="?", help="usuario/repositorio")
    s.add_argument("--create", metavar="NOMBRE", help="crear un repositorio privado nuevo")
    s.add_argument("--device", help="nombre de este dispositivo")
    sub.add_parser("info", help="configuración actual")
    sub.add_parser("list", help="juegos y su estado")
    s = sub.add_parser("status", help="estado resumido")
    s.add_argument("games", nargs="*")
    for name, hlp in (("sync", "sincronizar (sube o baja lo más nuevo)"),
                      ("push", "forzar subir la versión de aquí"), ("pull", "forzar bajar la de GitHub")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("games", nargs="*")
        s.add_argument("--dry-run", action="store_true")
        if name == "sync":
            s.add_argument("--prefer", choices=["local", "remote"])
    s = sub.add_parser("run", help="sincroniza, abre el juego y sincroniza al cerrarlo")
    s.add_argument("id")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s = sub.add_parser("add", help="añadir un juego")
    s.add_argument("id")
    s.add_argument("path")
    s.add_argument("--name")
    s.add_argument("--system", default="PC")
    s.add_argument("--include", nargs="+")
    s.add_argument("--exclude", nargs="+")
    s.add_argument("--collection", action="store_true", help="carpeta de emulador con varios juegos")
    s = sub.add_parser("set-path", help="cambiar la carpeta de un juego en este dispositivo")
    s.add_argument("id")
    s.add_argument("path")
    s.add_argument("--only-here", action="store_true", help="solo para este dispositivo")
    s = sub.add_parser("scan", help="detectar partidas en este dispositivo")
    s.add_argument("--add", action="store_true")
    s = sub.add_parser("remove", help="quitar un juego del catálogo")
    s.add_argument("id")
    s.add_argument("--delete-remote", action="store_true")
    s = sub.add_parser("history", help="versiones en GitHub")
    s.add_argument("id")
    s.add_argument("-n", type=int, default=15)
    s = sub.add_parser("restore", help="recuperar una versión anterior")
    s.add_argument("id")
    s.add_argument("commit")
    s = sub.add_parser("backups", help="copias de seguridad locales")
    s.add_argument("--all", action="store_true")
    return p


HANDLERS = {
    "login": cmd_login, "logout": cmd_logout, "repo": cmd_repo, "info": cmd_info, "list": cmd_list,
    "status": cmd_status, "sync": cmd_sync, "push": lambda s, a: cmd_sync(s, a, "push"),
    "pull": lambda s, a: cmd_sync(s, a, "pull"), "run": cmd_run, "add": cmd_add, "set-path": cmd_set_path,
    "scan": cmd_scan, "remove": cmd_remove, "history": cmd_history, "restore": cmd_restore, "backups": cmd_backups,
}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    ss = core.SaveSync(log=info)
    try:
        rc = HANDLERS[args.cmd](ss, args)
        return rc if isinstance(rc, int) else 0
    except core.SaveSyncError as e:
        print("Error: {}".format(e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelado.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
