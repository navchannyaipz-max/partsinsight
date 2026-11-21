import json, os, pathlib, shutil, sqlite3, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeGitHub(BaseHTTPRequestHandler):
    repos = {}
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer good-token": return self._send(401, {})
        if self.path == "/user": return self._send(200, {"login": "tester", "id": 42, "name": "Test User"})
        self._send(404, {})
    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer good-token": return self._send(401, {})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeGitHub.repos[body["name"]] = body
        self._send(201, {"html_url": f"https://github.com/tester/{body['name']}", "clone_url": "unused"})


def _project_copy(tmp):
    dst = tmp / "proj"
    shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.db", ".pytest_cache", "runs"))
    return dst


def _serve():
    srv = HTTPServer(("127.0.0.1", 0), FakeGitHub); threading.Thread(target=srv.serve_forever, daemon=True).start(); return srv


def test_publish_creates_repo_and_pushes_without_leaking_token(tmp_path):
    proj = _project_copy(tmp_path); bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True)
    srv = _serve()
    env = dict(os.environ, PI_TEST_TOKEN="good-token")
    r = subprocess.run([sys.executable, str(ROOT / "tools/publish.py"), "--root", str(proj), "--token-env", "PI_TEST_TOKEN", "--repo", "pi demo", "--yes",
                        "--api-base", f"http://127.0.0.1:{srv.server_port}", "--remote-url", str(bare)], capture_output=True, text=True, env=env)
    srv.shutdown()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "https://github.com/tester/pi-demo" in r.stdout and "good-token" not in r.stdout + r.stderr
    assert "pi-demo" in FakeGitHub.repos and FakeGitHub.repos["pi-demo"]["private"] is False
    log = subprocess.run(["git", "--git-dir", str(bare), "log", "--oneline"], capture_output=True, text=True).stdout
    assert "Додано посилання" in log
    files = subprocess.run(["git", "--git-dir", str(bare), "ls-tree", "-r", "--name-only", "main"], capture_output=True, text=True).stdout
    assert "docs/LINKS.md" in files and ".db" not in files
    assert "good-token" not in (proj / ".git" / "config").read_text()


def test_publish_blocks_database_and_secrets(tmp_path):
    proj = _project_copy(tmp_path)
    (proj / "leak.txt").write_text("key = ghp_" + "a" * 30)
    srv = _serve()
    r = subprocess.run([sys.executable, str(ROOT / "tools/publish.py"), "--root", str(proj), "--token-env", "PI_TEST_TOKEN", "--yes",
                        "--api-base", f"http://127.0.0.1:{srv.server_port}"], capture_output=True, text=True, env=dict(os.environ, PI_TEST_TOKEN="good-token"))
    srv.shutdown()
    assert r.returncode != 0 and "ЗУПИНЕНО" in (r.stdout + r.stderr) and "leak.txt" in (r.stdout + r.stderr)


def test_publish_rejects_bad_token(tmp_path):
    proj = _project_copy(tmp_path); srv = _serve()
    r = subprocess.run([sys.executable, str(ROOT / "tools/publish.py"), "--root", str(proj), "--token-env", "PI_TEST_TOKEN", "--yes",
                        "--api-base", f"http://127.0.0.1:{srv.server_port}"], capture_output=True, text=True, env=dict(os.environ, PI_TEST_TOKEN="bad"))
    srv.shutdown()
    assert r.returncode != 0 and "не прийняв токен" in (r.stdout + r.stderr)


def _mk_db(data_dir, rows):
    env = dict(os.environ, PARTSINSIGHT_DATA=str(data_dir))
    code = ("import sys; sys.path.insert(0, %r)\nfrom app import db\ncon=db.connect(db.default_path()); db.init(con, 'http://x')\n%s\ncon.commit()" % (str(ROOT), rows))
    subprocess.run([sys.executable, "-c", code], check=True, env=env)
    return env


def test_export_strips_secrets_and_merge_keeps_data(tmp_path):
    rows = ("con.execute(\"INSERT INTO settings VALUES('SERPAPI_KEY','secret123')\")\n"
            "con.execute(\"INSERT INTO settings VALUES('admin_password_hash','h')\")\n"
            "con.execute(\"INSERT INTO parts(norm_mpn,brand_key,mpn,title,first_seen,last_seen) VALUES('GDB1550','TRW','GDB1550','Колодки','2026-01-01','2026-01-02')\")\n"
            "con.execute(\"INSERT INTO price_snapshots(part_id,source,price,currency,fetched_at) VALUES(1,'s',100,'UAH','2026-01-02T10:00:00')\")\n"
            "con.execute(\"INSERT INTO searches(q,at,n_offers,n_sources,ms) VALUES('x','2026-01-01',1,1,5)\")")
    env = _mk_db(tmp_path / "local", rows)
    out = tmp_path / "exp"
    r = subprocess.run([sys.executable, str(ROOT / "tools/migrate_data.py"), "export", "--out", str(out)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    c = sqlite3.connect(out / "partsinsight.db")
    assert c.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0 and c.execute("SELECT COUNT(*) FROM searches").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM parts").fetchone()[0] == 1 and c.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0] == 1
    # merge у «хостингову» базу, де вже є та сама деталь і інша
    host = _mk_db(tmp_path / "host", "con.execute(\"INSERT INTO parts(norm_mpn,brand_key,mpn,title,first_seen,last_seen) VALUES('OTHER1','X','OTHER1','Інша','2026-01-01','2026-01-01')\")")
    for _ in range(2):  # повторне злиття не дублює
        r = subprocess.run([sys.executable, str(ROOT / "tools/migrate_data.py"), "merge", str(out / "partsinsight.db")], capture_output=True, text=True, env=host)
        assert r.returncode == 0, r.stderr
    hc = sqlite3.connect(pathlib.Path(host["PARTSINSIGHT_DATA"]) / "partsinsight.db")
    assert hc.execute("SELECT COUNT(*) FROM parts").fetchone()[0] == 2
    assert hc.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0] == 1
