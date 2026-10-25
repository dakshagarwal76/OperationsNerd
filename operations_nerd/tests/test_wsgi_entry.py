"""
WSGI entry tests: the app served through wsgi.py (a2wsgi) the way PythonAnywhere
runs it, in production mode, from a different working directory and with the
database at a custom DATABASE_PATH.

Uses the standard library's wsgiref server in a thread, so it needs nothing beyond
requirements.txt. Run like the others: python tests/test_wsgi_entry.py from operations_nerd/.
"""

import http.client
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from wsgiref.simple_server import WSGIRequestHandler, make_server

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

# Everything below is set before wsgi.py (and so main.py) is imported, because
# the database path and the production checks are read at import time.
WORKDIR = tempfile.mkdtemp()
DB_FILE = os.path.join(WORKDIR, "home", "data", "operations_nerd.db")    # directory does not exist yet
os.environ.update({
    "APP_ENV": "production",
    "SESSION_SECRET": "x" * 40,
    "LLM_PROVIDER": "stub",
    "SIGNUP_INVITE_CODE": "let-me-in",
    "RATE_LIMIT_ENABLED": "0",
    "DATABASE_PATH": DB_FILE,
})
os.environ.pop("CONFIG_AGENT_ENABLED", None)
os.chdir(WORKDIR)                       # not operations_nerd/: wsgi.py has to fix this itself
sys.path.insert(0, ROOT)

import wsgi                            # noqa: E402
from agentic import chat               # noqa: E402
from agentic.anthropic_llm import LLMResult   # noqa: E402

PASSWORD = "correct horse battery"


class _Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


_server = make_server("127.0.0.1", 0, wsgi.application, handler_class=_Quiet)
PORT = _server.server_address[1]
threading.Thread(target=_server.serve_forever, daemon=True).start()


class Client:
    def __init__(self):
        self.cookie = None

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if self.cookie:
            headers["Cookie"] = self.cookie
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        set_cookie = resp.getheader("Set-Cookie")
        conn.close()
        if set_cookie:
            pair = set_cookie.split(";")[0]
            self.cookie = pair if pair.split("=", 1)[1] else None     # a cleared cookie is empty
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = raw
        return resp.status, parsed, set_cookie


def signup(c, email="owner@example.com"):
    return c.call("POST", "/api/auth/signup", {"email": email, "password": PASSWORD, "invite_code": "let-me-in"})


def test_startup_created_the_database_at_database_path():
    assert os.path.isfile(DB_FILE), "init_db should create the file, and its missing directory, at DATABASE_PATH"
    with sqlite3.connect(DB_FILE) as conn:
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert "idx_sessions_expires" in indexes
    assert mode == "wal"
    assert os.getcwd() == os.path.abspath(os.path.join(ROOT))      # wsgi.py moved us into operations_nerd/


def test_health_and_app_page_load():
    c = Client()
    status, body, _ = c.call("GET", "/healthz")
    assert status == 200 and body == {"status": "ok"}
    status, body, _ = c.call("GET", "/app/")
    assert status == 200 and b"<html" in body.lower()
    status, body, _ = c.call("GET", "/")
    assert status == 200 and body["service"] == "Operations Nerd"


def test_legacy_routes_are_404_in_production():
    c = Client()
    for path in ("/ui/", "/packs", "/packs/realestate/questionnaire", "/businesses/1/state", "/agent/"):
        status, _, _ = c.call("GET", path)
        assert status == 404, f"{path} should be 404 in production, got {status}"
    assert c.call("POST", "/businesses", {"name": "x"})[0] == 404


def test_signup_login_logout_and_cookie_flags():
    c = Client()
    status, body, set_cookie = signup(c)
    assert status == 201 and body["email"] == "owner@example.com"
    for flag in ("HttpOnly", "Secure", "SameSite=lax"):
        assert flag.lower() in set_cookie.lower(), f"{flag} missing from {set_cookie}"
    assert c.call("GET", "/api/auth/me")[0] == 200

    assert c.call("POST", "/api/auth/logout")[0] == 200
    assert c.call("GET", "/api/auth/me")[0] == 401

    assert c.call("POST", "/api/auth/login", {"email": "owner@example.com", "password": "wrong password!"})[0] == 401
    status, _, set_cookie = c.call("POST", "/api/auth/login", {"email": "owner@example.com", "password": PASSWORD})
    assert status == 200 and "secure" in set_cookie.lower()
    assert c.call("GET", "/api/auth/me")[0] == 200
    assert Client().call("GET", "/api/auth/me")[0] == 401       # no cookie, no session


def test_signup_needs_the_invite_code():
    status, _, _ = Client().call("POST", "/api/auth/signup",
                                 {"email": "nope@example.com", "password": PASSWORD, "invite_code": "guess"})
    assert status == 403


def test_one_chat_turn_and_approve():
    c = Client()
    assert signup(c, "chat@example.com")[0] == 201
    status, biz, _ = c.call("POST", "/api/businesses", {"name": "Gym", "template": "health_club"})
    assert status == 201
    lead = next(e for e in biz["spec"]["entities"] if e["label"] == "Lead")["key"]

    def fake_llm(system, messages, schema):
        ops = [{"op": "add_field", "entity": lead, "label": "Referral source", "type": "text"}]
        return LLMResult(json.dumps({"reply": "Adding it.", "operations": ops}), "stub", 5, 5, 1)

    original, chat.run_llm = chat.run_llm, fake_llm
    try:
        status, proposal, _ = c.call("POST", f"/api/businesses/{biz['id']}/chat",
                                     {"messages": [{"role": "user", "content": "track referral source on leads"}]})
    finally:
        chat.run_llm = original
    assert status == 200 and proposal["status"] == "pending" and len(proposal["operations"]) == 1

    status, applied, _ = c.call("POST", f"/api/proposals/{proposal['proposal_id']}/approve")
    assert status == 200, applied
    status, versions, _ = c.call("GET", f"/api/businesses/{biz['id']}/versions")
    assert status == 200 and len(versions) == 2


def test_login_sweeps_expired_sessions():
    c = Client()
    assert signup(c, "sweep@example.com")[0] == 201
    long_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with sqlite3.connect(DB_FILE) as conn:
        account_id = conn.execute("SELECT id FROM accounts WHERE email = 'sweep@example.com'").fetchone()[0]
        for i in range(3):
            conn.execute("INSERT INTO sessions (account_id, token_hash, expires_at) VALUES (?, ?, ?)",
                         (account_id, f"stale-{i}", long_ago))
    c.call("POST", "/api/auth/login", {"email": "sweep@example.com", "password": PASSWORD})
    with sqlite3.connect(DB_FILE) as conn:
        left = conn.execute("SELECT COUNT(*) FROM sessions WHERE token_hash LIKE 'stale-%'").fetchone()[0]
        live = conn.execute("SELECT COUNT(*) FROM sessions WHERE account_id = ?", (account_id,)).fetchone()[0]
    assert left == 0 and live >= 1


def test_oversized_body_is_refused_through_wsgi():
    # The server may answer 413 and close the connection before the client has finished sending the 2 MB body.
    # The client then sees a broken or reset connection instead of the response (this depends on timing and the
    # OS), so that counts as a pass too, but only if the server is seen to have answered 413.
    sent = []
    app = _server.get_app()

    def recording(environ, start_response):
        def start(status, headers, exc_info=None):
            if environ.get("PATH_INFO") == "/api/auth/signup":
                sent.append(status)
            return start_response(status, headers, exc_info)
        return app(environ, start)

    _server.set_app(recording)
    try:
        c = Client()
        try:
            status, _, _ = c.call("POST", "/api/auth/signup", {"email": "big@example.com", "password": "x" * 2_000_000,
                                                               "invite_code": "let-me-in"})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as e:
            status = None
            for _ in range(100):                     # the server thread may still be finishing the response
                if sent:
                    break
                threading.Event().wait(0.05)
            assert sent and sent[0].startswith("413"), f"{type(e).__name__} without a 413 from the server: {sent}"
    finally:
        _server.set_app(app)
    assert status in (413, None), status
    assert not sent or sent[0].startswith("413"), sent


class Skip(Exception):
    pass


FORK_CHILD_SECONDS = 5

# Fork; in the child call the WSGI app for GET /healthz and exit 0 only if it
# answers 200 within FORK_CHILD_SECONDS (SIGALRM ends a hung child). uWSGI does
# exactly this: import the WSGI file in the master, then fork the worker.
# Kept as source so the in-process test and the fresh-interpreter test share it.
FORK_HELPER = f"""
import os, signal
from wsgiref.util import setup_testing_defaults

def healthz_in_forked_child(application):
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            signal.alarm({FORK_CHILD_SECONDS})
            environ = {{"REQUEST_METHOD": "GET", "PATH_INFO": "/healthz", "CONTENT_LENGTH": ""}}
            setup_testing_defaults(environ)
            seen = {{}}
            body = b"".join(application(environ, lambda status, headers, exc=None: seen.setdefault("status", status)))
            code = 0 if seen.get("status", "").startswith("200") and b"ok" in body else 2
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)
"""


def test_forked_child_serves_after_the_parent_already_built_the_wrapper():
    if not hasattr(os, "fork"):
        raise Skip("os.fork is not available on this platform")
    assert Client().call("GET", "/healthz")[0] == 200            # the parent builds its wrapper and loop thread
    namespace = {}
    exec(FORK_HELPER, namespace)
    assert namespace["healthz_in_forked_child"](wsgi.application) == 0,         "a forked child must not reuse the parent's dead event-loop thread"


def test_forked_child_serves_when_the_wrapper_was_never_used_before_the_fork():
    if not hasattr(os, "fork"):
        raise Skip("os.fork is not available on this platform")
    script = FORK_HELPER + f"""
import sys
sys.path.insert(0, {ROOT!r})
import wsgi                                  # the uWSGI master imports the WSGI file...
sys.exit(healthz_in_forked_child(wsgi.application))      # ...then forks the worker, before any request
"""
    done = subprocess.run([sys.executable, "-c", script], cwd=WORKDIR, capture_output=True, text=True,
                          timeout=FORK_CHILD_SECONDS * 6)
    assert done.returncode == 0, f"forked child did not answer /healthz (exit {done.returncode}): {done.stderr[-1500:]}"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = skipped = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS", name)
        except Skip as e:
            skipped += 1
            print("SKIP", name, "-", e)
        except Exception as e:
            failed += 1
            print("FAIL", name, "-", repr(e))
    _server.shutdown()
    print(f"\n{len(tests) - failed - skipped} tests passed" + (f", {skipped} skipped" if skipped else "")
          + (f", {failed} FAILED" if failed else ""))
    sys.exit(1 if failed else 0)
