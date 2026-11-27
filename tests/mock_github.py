"""Imitación mínima de la API de GitHub (git data API + device flow) para pruebas.

    python tests/mock_github.py 8900      # tokens válidos: "tok-cesar"
"""
import base64
import hashlib
import json
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKENS = {"tok-cesar": "CesarAcostaPy"}
BLOBS = {}
REPOS = {}
LOCK = threading.Lock()
RACE = {"pending": False}
DEVICE = {"polls": 0}
COUNTER = [0]


def sha1(data):
    return hashlib.sha1(data).hexdigest()


def blob_sha(data):
    return sha1(b"blob %d\0" % len(data) + data)


def new_repo(full, private=True, auto_init=False):
    REPOS[full] = {"private": private, "branch": "main", "refs": {}, "commits": {}, "trees": {}}
    if auto_init:
        commit_files(full, {"README.md": b"# init\n"}, "Initial commit")


def store_tree(repo, mapping):
    sha = sha1(json.dumps(sorted(mapping.items())).encode())
    repo["trees"][sha] = dict(mapping)
    return sha


def make_commit(repo, tree, parents, message):
    COUNTER[0] += 1
    sha = sha1("{}{}{}{}".format(tree, parents, message, COUNTER[0]).encode())
    repo["commits"][sha] = {"tree": tree, "parents": parents, "message": message,
                            "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    return sha


def commit_files(full, files, message):
    repo = REPOS[full]
    head = repo["refs"].get(repo["branch"])
    mapping = dict(repo["trees"][repo["commits"][head]["tree"]]) if head else {}
    for p, data in files.items():
        s = blob_sha(data)
        BLOBS[s] = data
        mapping[p] = s
    tree = store_tree(repo, mapping)
    c = make_commit(repo, tree, [head] if head else [], message)
    repo["refs"][repo["branch"]] = c
    return c


LATENCY = float(__import__("os").environ.get("MOCK_LATENCY", "0"))
STATS = {"requests": 0, "connections": 0}


def git_sha(kind, data):
    return sha1("{} {}\0".format(kind, len(data)).encode() + data)


def parse_pack(data):
    assert data[:4] == b"PACK", "no es un paquete"
    n = int.from_bytes(data[8:12], "big")
    assert hashlib.sha1(data[:-20]).digest() == data[-20:], "checksum del paquete"
    i, objs = 12, []
    for _ in range(n):
        c = data[i]; i += 1
        kind = (c >> 4) & 7
        size, shift = c & 15, 4
        while c & 0x80:
            c = data[i]; i += 1
            size |= (c & 0x7F) << shift
            shift += 7
        d = zlib.decompressobj()
        raw = d.decompress(data[i:])
        i = len(data) - len(d.unused_data)
        assert len(raw) == size, "tamaño de objeto"
        objs.append(({1: "commit", 2: "tree", 3: "blob"}[kind], raw))
    return objs


def parse_tree(data):
    out, i = [], 0
    while i < len(data):
        sp = data.index(b" ", i)
        nul = data.index(b"\0", sp)
        mode, name = data[i:sp].decode(), data[sp + 1:nul].decode()
        out.append((mode, name, data[nul + 1:nul + 21].hex()))
        i = nul + 21
    names = [n + ("/" if m == "40000" else "") for m, n, _ in out]
    assert names == sorted(names, key=lambda x: x.encode()), "treeNotSorted"
    return out


def flatten(trees, sha, prefix=""):
    flat = {}
    for mode, name, s in trees[sha]:
        if mode == "40000":
            flat.update(flatten(trees, s, prefix + name + "/"))
        else:
            if s not in BLOBS:
                raise KeyError("falta el blob " + s)
            flat[prefix + name] = s
    return flat


def pkt(b):
    return "{:04x}".format(len(b) + 4).encode() + b


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, como GitHub

    def setup(self):
        super().setup()
        STATS["connections"] += 1

    def log_message(self, *a):
        pass

    def send(self, code, obj=None, raw=None):
        data = raw if raw is not None else json.dumps(obj if obj is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        ct = self.headers.get("Content-Type", "")
        if "json" in ct:
            return json.loads(raw or b"{}")
        return {k: v[0] for k, v in parse_qs(raw.decode()).items()}

    def auth(self):
        a = self.headers.get("Authorization", "")
        return TOKENS.get(a.split(" ", 1)[1]) if " " in a else None

    def handle_any(self, method):
        STATS["requests"] += 1
        if LATENCY:
            time.sleep(LATENCY)
        u = urlparse(self.path)
        if u.path == "/_admin/stats":
            return self.send(200, STATS)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = [p for p in u.path.split("/") if p]
        with LOCK:
            # device flow (web)
            if u.path == "/login/device/code":
                return self.send(200, {"device_code": "dc1", "user_code": "ABCD-1234",
                                       "verification_uri": "https://github.com/login/device",
                                       "expires_in": 900, "interval": 1})
            if u.path == "/login/oauth/access_token":
                DEVICE["polls"] += 1
                if DEVICE["polls"] < 2:
                    return self.send(200, {"error": "authorization_pending"})
                return self.send(200, {"access_token": "tok-cesar", "token_type": "bearer"})
            if u.path == "/_admin/race":
                RACE["pending"] = True
                return self.send(200, {})
            if ".git/" in u.path:
                return self.git_endpoint(u, parts)
            user = self.auth()
            if not user:
                return self.send(401, {"message": "Bad credentials"})
            if u.path == "/user":
                return self.send(200, {"login": user})
            if u.path == "/user/repos":
                if method == "POST":
                    b = self.body()
                    full = "{}/{}".format(user, b["name"])
                    if full in REPOS:
                        return self.send(422, {"message": "name already exists"})
                    new_repo(full, b.get("private", True), b.get("auto_init", False))
                    return self.send(201, {"full_name": full, "private": True, "default_branch": "main"})
                return self.send(200, [{"full_name": f, "name": f.split("/")[1], "private": r["private"],
                                        "owner": {"login": f.split("/")[0]}, "default_branch": "main",
                                        "permissions": {"push": True}, "updated_at": "2026-11-24T00:00:00Z"}
                                       for f, r in REPOS.items()])
            if len(parts) >= 3 and parts[0] == "repos":
                full = parts[1] + "/" + parts[2]
                repo = REPOS.get(full)
                if not repo or repo.get("noaccess"):
                    return self.send(404, {"message": "Not Found"})
                rest = parts[3:]
                if not rest:
                    return self.send(200, {"full_name": full, "private": repo["private"],
                                           "default_branch": repo["branch"], "permissions": {"push": True}})
                if rest[:3] == ["git", "ref", "heads"]:
                    if not repo["commits"]:
                        return self.send(409, {"message": "Git Repository is empty."})
                    head = repo["refs"].get(rest[3])
                    if not head:
                        return self.send(404, {"message": "Not Found"})
                    return self.send(200, {"object": {"sha": head}})
                if rest[0] == "contents" and method == "PUT":
                    b = self.body()
                    commit_files(full, {"/".join(rest[1:]): base64.b64decode(b["content"])}, b["message"])
                    return self.send(201, {})
                if rest[:2] == ["git", "commits"] and method == "GET":
                    c = repo["commits"].get(rest[2])
                    if not c:
                        return self.send(404, {"message": "Not Found"})
                    return self.send(200, {"sha": rest[2], "tree": {"sha": c["tree"]}, "message": c["message"]})
                if rest[:2] == ["git", "trees"] and method == "GET":
                    t = repo["trees"].get(rest[2])
                    if t is None:
                        return self.send(404, {"message": "Not Found"})
                    return self.send(200, {"sha": rest[2], "truncated": False,
                                           "tree": [{"path": p, "type": "blob", "sha": s, "size": len(BLOBS[s])}
                                                    for p, s in t.items()]})
                if rest[:2] == ["git", "blobs"]:
                    if method == "POST":
                        b = self.body()
                        data = base64.b64decode(b["content"])
                        s = blob_sha(data)
                        BLOBS[s] = data
                        return self.send(201, {"sha": s})
                    data = BLOBS.get(rest[2])
                    if data is None:
                        return self.send(404, {"message": "Not Found"})
                    if "raw" in self.headers.get("Accept", ""):
                        return self.send(200, raw=data)
                    return self.send(200, {"content": base64.b64encode(data).decode(), "encoding": "base64"})
                if rest[:2] == ["git", "trees"] and method == "POST":
                    b = self.body()
                    mapping = dict(repo["trees"].get(b.get("base_tree"), {}))
                    for e in b["tree"]:
                        if e["sha"] is None:
                            mapping.pop(e["path"], None)
                        else:
                            if e["sha"] not in BLOBS:
                                return self.send(422, {"message": "blob missing"})
                            mapping[e["path"]] = e["sha"]
                    return self.send(201, {"sha": store_tree(repo, mapping)})
                if rest[:2] == ["git", "commits"] and method == "POST":
                    b = self.body()
                    return self.send(201, {"sha": make_commit(repo, b["tree"], b["parents"], b["message"])})
                if rest[:3] == ["git", "refs", "heads"] and method == "PATCH":
                    b = self.body()
                    if RACE["pending"]:
                        RACE["pending"] = False
                        commit_files(full, {"devices/otro.json": b'{"device":"otro","platform":"linux","games":{}}'},
                                     "otro dispositivo")
                    head = repo["refs"][rest[3]]
                    if head not in repo["commits"][b["sha"]]["parents"] and not b.get("force"):
                        return self.send(422, {"message": "Update is not a fast forward"})
                    repo["refs"][rest[3]] = b["sha"]
                    return self.send(200, {"object": {"sha": b["sha"]}})
                if rest == ["commits"]:
                    path = q.get("path", "")
                    n = int(q.get("per_page", 30))
                    out, c = [], repo["refs"].get(repo["branch"])
                    while c and len(out) < n:
                        cm = repo["commits"][c]
                        par = cm["parents"][0] if cm["parents"] else None

                        def sub(commit):
                            if not commit:
                                return {}
                            t = repo["trees"][repo["commits"][commit]["tree"]]
                            return {p: s for p, s in t.items() if p == path or p.startswith(path + "/")}
                        if sub(c) != sub(par):
                            out.append({"sha": c, "commit": {"message": cm["message"],
                                                             "committer": {"date": cm["date"]}}})
                        c = par
                    return self.send(200, out)
                if rest[:2] == ["releases", "latest"]:
                    return self.send(404, {"message": "Not Found"})
            return self.send(404, {"message": "Not Found: " + u.path})

    def git_endpoint(self, u, parts):
        a = self.headers.get("Authorization", "")
        try:
            user_pass = base64.b64decode(a.split(" ", 1)[1]).decode() if a.startswith("Basic ") else ""
        except Exception:
            user_pass = ""
        if user_pass.split(":", 1)[-1] not in TOKENS:
            self.send_response(401); self.send_header("Content-Length", "0"); self.end_headers(); return
        full = parts[0] + "/" + parts[1][:-4]
        repo = REPOS.get(full)
        if not repo or repo.get("noaccess"):
            return self.send(404, {"message": "Not Found"})
        if u.path.endswith("/info/refs"):
            head = repo["refs"].get(repo["branch"])
            caps = b"report-status delete-refs side-band-64k quiet atomic ofs-delta agent=mock"
            body = pkt(b"# service=git-receive-pack\n") + b"0000"
            if head:
                body += pkt(head.encode() + b" refs/heads/" + repo["branch"].encode() + b"\0" + caps + b"\n")
            else:
                body += pkt(b"0" * 40 + b" capabilities^{}\0" + caps + b"\n")
            body += b"0000"
            return self.send(200, raw=body)
        if u.path.endswith("/git-receive-pack"):
            n = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(n)
            ln = int(data[:4], 16)
            cmd = data[4:ln].split(b"\0")[0].decode().split()
            old, new, ref = cmd
            pack = data[ln + 4:]
            STATS["pushes"] = STATS.get("pushes", 0) + 1
            STATS["push_bytes"] = STATS.get("push_bytes", 0) + len(pack)
            if RACE["pending"]:
                RACE["pending"] = False
                commit_files(full, {"devices/otro.json": b'{"device":"otro","platform":"linux","games":{}}'},
                             "otro dispositivo")
            trees, commits = {}, {}
            for kind, raw in parse_pack(pack):
                sha = git_sha(kind, raw)
                if kind == "blob":
                    BLOBS[sha] = raw
                elif kind == "tree":
                    trees[sha] = parse_tree(raw)
                else:
                    commits[sha] = raw.decode()
            head = repo["refs"].get(repo["branch"])
            if (head or "0" * 40) != old:
                body = pkt(b"unpack ok\n") + pkt(("ng " + ref + " fetch first\n").encode()) + b"0000"
                return self.send(200, raw=body)
            text = commits[new]
            tree = text.split("\n")[0].split()[1]
            parents = [ln.split()[1] for ln in text.split("\n") if ln.startswith("parent ")]
            msg = text.split("\n\n", 1)[1].strip()
            flat = flatten(trees, tree)
            repo["trees"][tree] = flat
            repo["commits"][new] = {"tree": tree, "parents": parents, "message": msg,
                                    "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            repo["refs"][repo["branch"]] = new
            return self.send(200, raw=pkt(b"unpack ok\n") + pkt(("ok " + ref + "\n").encode()) + b"0000")
        return self.send(404, {"message": "Not Found"})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PATCH(self):
        self.handle_any("PATCH")

    def do_PUT(self):
        self.handle_any("PUT")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8900
    new_repo("CesarAcostaPy/mis-partidas", private=True, auto_init=False)  # vacío, como uno recién creado
    new_repo("CesarAcostaPy/sin-acceso", private=True, auto_init=True)
    REPOS["CesarAcostaPy/sin-acceso"]["noaccess"] = True  # aparece en la lista pero el token no puede usarlo
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
