"""Serve the dashboard through the authenticated fnOS Unix-socket gateway."""
from datetime import datetime, timezone
import json
import os
import pwd
import re
import socket
import struct
import socketserver
import sys
from time import monotonic
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

BASE = "/app/sas-monitor"
WEB = Path(__file__).resolve().parent / "www"
SNAPSHOT = Path("/run/sas-monitor/snapshot.json")
REQUEST = Path("/run/sas-monitor-refresh/request")
SCAN_SOCKET = "/run/sas-monitor-scan.sock"
SCAN_ROUTE = re.compile(r"/api/scan/([0-9a-f]{64})(?:/(start)|/stop/([0-9a-f]{32}))?")
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "application/javascript; charset=utf-8"),
}


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(8)
        super().setup()

    def log_message(self, format, *args):
        pass

    def send_data(self, status, kind, body):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def authorize(self):
        user_ids = self.headers.get_all("X-Trim-Userid") or []
        if (self.headers.get_all("X-Trim-Isadmin") != ["true"] or
                len(user_ids) != 1 or not user_ids[0].isascii() or not user_ids[0].isdigit()):
            self.send_data(403, "text/plain; charset=utf-8", "请使用飞牛管理员账号打开应用。".encode())
            return False
        return True

    def do_GET(self):
        if not self.authorize():
            return
        path = urlsplit(self.path).path
        if path == BASE:
            self.send_response(308)
            self.send_header("Location", BASE + "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not path.startswith(BASE + "/"):
            self.send_error(404)
            return

        route = path[len(BASE):]
        if route == "/api/snapshot":
            self.snapshot()
        elif (scan := SCAN_ROUTE.fullmatch(route)) and not any(scan.groups()[1:]):
            self.scan_command(["status", scan[1]])
        elif route in ASSETS:
            filename, kind = ASSETS[route]
            self.send_data(200, kind, (WEB / filename).read_bytes())
        else:
            self.send_error(404)

    def send_json(self, status, payload):
        self.send_data(status, "application/json; charset=utf-8", json.dumps(payload).encode())

    def do_POST(self):
        if not self.authorize():
            return
        path = urlsplit(self.path).path
        scan = SCAN_ROUTE.fullmatch(path[len(BASE):]) if path.startswith(BASE + "/") else None
        is_scan = scan and (scan[2] or scan[3])
        if not is_scan and path != BASE + "/api/refresh":
            self.send_error(404)
            return
        # The same-origin UI sends this custom header. Cross-origin forms cannot set it.
        header = "X-SAS-Scan" if is_scan else "X-SAS-Refresh"
        if (self.headers.get_all(header) != ["1"] or
                self.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin"):
            self.send_json(403, {"state": "forbidden"})
            return
        if is_scan:
            self.scan_command(["start", scan[1]] if scan[2] else ["stop", scan[1], scan[3]])
            return

        now = monotonic()
        if now < getattr(self.server, "refresh_after", 0):
            self.send_json(429, {"state": "cooldown"})
            return
        requested_at = datetime.now(timezone.utc).isoformat()
        try:
            # Root's path unit watches this marker. Its contents carry no instructions.
            fd = os.open(REQUEST, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
        except FileExistsError:
            pass  # One outstanding request is enough to schedule the service.
        except FileNotFoundError:
            self.send_json(503, {"state": "collector_upgrade"})
            return
        except OSError:
            self.send_json(503, {"state": "collector_unavailable"})
            return
        else:
            os.close(fd)
        self.server.refresh_after = now + 10
        self.send_json(202, {"state": "queued", "requestedAt": requested_at})

    def scan_command(self, words):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(3)
                client.connect(SCAN_SOCKET)
                client.sendall(json.dumps(words).encode() + b"\n")
                with client.makefile("rb") as reader:
                    reply = reader.readline(16384)
            if not reply.endswith(b"\n"):
                raise ValueError("incomplete response")
            status, data = json.loads(reply)
            if status not in (200, 202, 400, 409, 503) or not isinstance(data, dict):
                raise ValueError("unexpected response")
        except FileNotFoundError:
            status, data = 503, {"error": "upgrade"}
        except (OSError, ValueError, TypeError):
            status, data = 503, {"error": "service"}
        self.send_json(status, data)

    def snapshot(self):
        try:
            payload = SNAPSHOT.read_bytes()
        except FileNotFoundError:
            payload = json.dumps({"state": "waiting", "disks": []}).encode()
        except OSError:
            payload = json.dumps({"state": "unavailable", "disks": []}).encode()
        self.send_data(200, "application/json; charset=utf-8", payload)


class Gateway(socketserver.UnixStreamServer):
    request_queue_size = 32

    def __init__(self, socket_path):
        super().__init__(socket_path, Handler, bind_and_activate=False)
        self.proxy_uids = {0, os.getuid()}
        for account in ("www-data", "nginx", "trim_nginx", "trim_app_cgi"):
            try:
                self.proxy_uids.add(pwd.getpwnam(account).pw_uid)
            except KeyError:
                pass
        previous = os.umask(0o077)
        try:
            self.server_bind()
            # Only trusted local peer UIDs may supply gateway identity headers.
            os.chmod(socket_path, 0o666)
            self.server_activate()
        finally:
            os.umask(previous)


    def verify_request(self, request, client_address):
        credentials = request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _, uid, _ = struct.unpack("3i", credentials)
        return uid in self.proxy_uids


if __name__ == "__main__":
    with Gateway(sys.argv[1]) as server:
        server.serve_forever()
