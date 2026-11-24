"""Imitación mínima de la API de GitHub (git data API + device flow) para pruebas.

    python tests/mock_github.py 8900      # tokens válidos: "tok-cesar"
"""
import base64
import hashlib
import json
import sys
import threading
import time
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


class H(BaseHTTPRequestHandler):
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
        u = urlparse(self.path)
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
                if not repo:
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
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
