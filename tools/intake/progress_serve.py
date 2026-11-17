"""Loopback-only, read-only HTTP view of the progress report (``progress-serve``)."""
import http.server
import ipaddress
import json
import socket
import threading
import time

from . import progress
from .contracts import IntakeError
from .progress_render import SCRIPT_SRC, render_html


class BindRefused(IntakeError):
    def __init__(self):
        super().__init__("non_loopback_bind")


def check_bind(host, allow_non_loopback=False):
    """Return the address to bind; refuse anything not loopback without approval."""
    if host == "localhost":
        return "127.0.0.1"
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if loopback:
        return str(ipaddress.ip_address(host))
    if allow_non_loopback and host:
        return host
    raise BindRefused()


class Latest:
    """At most one render at a time, shared by every request that arrives meanwhile.

    A request that finds a render running waits for it and uses its result, and a result younger
    than ``reuse`` seconds is served again, so any number of open tabs cost one render per
    ``reuse`` seconds plus one render's time at most, never one render per tab.
    """

    def __init__(self, render, reuse=1.0, clock=time.monotonic):
        self.render, self.reuse, self.clock = render, reuse, clock
        self.lock, self.result, self.finished, self.running = threading.Condition(), None, None, False

    def get(self):
        with self.lock:
            while True:
                if self.result is not None and not self.running \
                        and self.clock() - self.finished <= self.reuse:
                    return self.result
                if not self.running:
                    break
                joined = self.finished
                self.lock.wait()
                if self.result is not None and self.finished != joined:
                    return self.result
            self.running = True
        try:
            result = self.render()
        except BaseException:
            with self.lock:
                self.running = False
                self.lock.notify_all()
            raise
        with self.lock:
            self.result, self.finished, self.running = result, self.clock(), False
            self.lock.notify_all()
        return result


def make_server(root, *, host="127.0.0.1", port=8765, refresh=5, limit=50, runs=None,
                allow_non_loopback=False, reuse=1.0):
    address = check_bind(host, allow_non_loopback)
    latest = Latest(lambda: progress.snapshot(root, limit=limit, runs=runs), reuse)

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "librarian-progress"
        sys_version = ""

        def send(self, status, kind, body):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy",
                             f"default-src 'none'; style-src 'unsafe-inline'; script-src {SCRIPT_SRC}; "
                             "frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def trusted_host(self):
            if allow_non_loopback:
                return True
            name = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return name in {"127.0.0.1", "localhost", "::1"}

        def do_GET(self):
            if not self.trusted_host():
                return self.send(421, "text/plain; charset=utf-8", "Misdirected request\n")
            path = self.path.split("?", 1)[0]
            if path not in {"/", "/progress.json"}:
                return self.send(404, "text/plain; charset=utf-8", "Not found\n")
            report = latest.get()
            if path == "/":
                return self.send(200, "text/html; charset=utf-8", render_html(report, refresh=refresh))
            return self.send(200, "application/json", json.dumps(report, sort_keys=True))

        do_HEAD = do_GET

        def refuse(self):
            self.send(405, "text/plain; charset=utf-8", "Read-only view\n")

        do_POST = do_PUT = do_PATCH = do_DELETE = refuse

        def log_message(self, *args):
            return

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        address_family = socket.AF_INET6 if ":" in address else socket.AF_INET

    return Server((address, port), Handler)


def serve(root, **options):
    server = make_server(root, **options)
    host, port = server.server_address[:2]
    shown = f"[{host}]" if ":" in host else host
    print(json.dumps({"serving": f"http://{shown}:{port}/", "read_only": True}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return {"stopped": True}
