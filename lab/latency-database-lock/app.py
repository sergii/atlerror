import json
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DB_HOST = os.environ.get("DB_HOST", "db")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_USER = os.environ.get("DB_USER", "atlerror")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "atlerror")
DB_NAME = os.environ.get("DB_NAME", "atlerror")


def pg_env(application_name: str):
    env = dict(os.environ)
    env["PGPASSWORD"] = DB_PASSWORD
    env["PGAPPNAME"] = application_name
    return env


def psql_command(sql: str, application_name: str, timeout=10):
    command = [
        "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
        "-h", DB_HOST, "-p", DB_PORT,
        "-U", DB_USER, "-d", DB_NAME,
        "-c", sql,
    ]
    return subprocess.run(
        command,
        env=pg_env(application_name),
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )


def psql_popen(sql: str, application_name: str):
    command = [
        "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
        "-h", DB_HOST, "-p", DB_PORT,
        "-U", DB_USER, "-d", DB_NAME,
        "-c", sql,
    ]
    return subprocess.Popen(
        command,
        env=pg_env(application_name),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def scalar(sql: str) -> str:
    result = psql_command(sql, "atlerror_observer")
    return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""


def initialize():
    psql_command(
        "CREATE TABLE IF NOT EXISTS atlerror_lock_lab (id integer PRIMARY KEY, value integer NOT NULL); "
        "INSERT INTO atlerror_lock_lab(id, value) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;",
        "atlerror_setup",
    )


def wait_for_locker_sleep(timeout_seconds=2.0):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        count = scalar(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE application_name = 'atlerror_locker' AND wait_event = 'PgSleep';"
        )
        if count == "1":
            return
        time.sleep(0.01)
    raise RuntimeError("locker did not acquire row lock and enter pg_sleep")


def observe_waiter_lock(waiter, timeout_seconds=0.20):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and waiter.poll() is None:
        count = scalar(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE application_name = 'atlerror_waiter' AND wait_event_type = 'Lock';"
        )
        if count == "1":
            return True
        time.sleep(0.01)
    return False


def run_unblocked_update():
    started = time.monotonic()
    psql_command(
        "UPDATE atlerror_lock_lab SET value = value + 1 WHERE id = 1;",
        "atlerror_waiter",
    )
    return (time.monotonic() - started) * 1000.0, False


def run_blocked_update(hold_ms: float):
    hold_seconds = max(hold_ms, 0.0) / 1000.0
    locker_sql = (
        "BEGIN; "
        "SELECT id FROM atlerror_lock_lab WHERE id = 1 FOR UPDATE; "
        f"SELECT pg_sleep({hold_seconds:.6f}); "
        "COMMIT;"
    )
    locker = psql_popen(locker_sql, "atlerror_locker")
    try:
        wait_for_locker_sleep()
        started = time.monotonic()
        waiter = psql_popen(
            "UPDATE atlerror_lock_lab SET value = value + 1 WHERE id = 1;",
            "atlerror_waiter",
        )
        lock_event_observed = observe_waiter_lock(waiter)
        stdout, stderr = waiter.communicate(timeout=5)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if waiter.returncode != 0:
            raise RuntimeError(f"waiter failed: {stderr or stdout}")
        locker_stdout, locker_stderr = locker.communicate(timeout=5)
        if locker.returncode != 0:
            raise RuntimeError(f"locker failed: {locker_stderr or locker_stdout}")
        return elapsed_ms, lock_event_observed
    finally:
        if locker.poll() is None:
            locker.terminate()
            try:
                locker.wait(timeout=1)
            except subprocess.TimeoutExpired:
                locker.kill()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def reply(self, status: int, payload):
        if isinstance(payload, str):
            body = payload.encode()
            content_type = "text/plain"
        else:
            body = json.dumps(payload).encode()
            content_type = "application/json"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.reply(200, "ok")
            return
        if parsed.path != "/work":
            self.reply(404, {"error": "not_found"})
            return
        try:
            params = parse_qs(parsed.query)
            hold_ms = float(params.get("hold_ms", ["0"])[0])
            if hold_ms > 0:
                wait_ms, lock_event = run_blocked_update(hold_ms)
            else:
                wait_ms, lock_event = run_unblocked_update()
            self.reply(200, {
                "database_lock_wait_ms": wait_ms,
                "lock_wait_event_observed": lock_event,
            })
        except Exception as error:
            self.reply(500, {"error": type(error).__name__, "message": str(error)})


initialize()
ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
