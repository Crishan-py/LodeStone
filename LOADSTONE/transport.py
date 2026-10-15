"""The localhost HTTP + SSE server that fronts the API."""
import http.server
import json
import logging
import queue
import sys
import threading
import urllib.request
import urllib.parse
import urllib.error
from .api import API
from .paths import RESOURCE_DIR, log_exception



class EventBus:
    """Fans API events out to every connected UI client.

    One bounded queue per subscriber instead of a shared callback list: each SSE
    handler thread blocks on its own queue, so a backgrounded browser tab can
    never stall the launcher or another client. A client whose queue fills up is
    dropped rather than allowed to apply back-pressure to the game loop.
    """

    def __init__(self):
        self._subs: set = set()
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=512)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs.discard(q)

    def is_active(self, q):
        with self._lock:
            return q in self._subs

    def client_count(self):
        with self._lock:
            return len(self._subs)

    def publish(self, name, data):
        frame = (name, data)
        with self._lock:
            targets = list(self._subs)
        for q in targets:
            try:
                q.put_nowait(frame)
            except queue.Full:
                with self._lock:
                    self._subs.discard(q)
class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves the UI and exposes the API over localhost HTTP.

    BaseHTTPRequestHandler builds one of these per request, so the shared
    collaborators arrive as class attributes set by serve().
    """

    protocol_version = "HTTP/1.1"

    api = None
    bus = None
    token = ""
    port = 0

    def log_message(self, fmt, *args):
        pass

    def _allowed_origins(self):
        return (f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}")

    def _deny(self, reason, status=403):
        self._send_json(status, {"success": False, "error": reason})

    def _reject(self, token, needs_token=True):
        """Block anything that is not this app talking to this app.

        The token alone would do, but a hostile page cannot read it, cannot send
        a preflighted cross-origin POST without our CORS headers, and is refused
        outright by the Host check -- so rebinding a domain to 127.0.0.1 buys
        an attacker nothing.
        """
        host = self.headers.get("Host", "")
        if host not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            self._deny("bad host")
            return True
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self._allowed_origins():
            self._deny("bad origin")
            return True
        if needs_token:
            import secrets
            # EventSource cannot set headers, so the token may arrive in the
            # query string; fetch() sends it as a header instead.
            supplied = token or self.headers.get("X-Loadstone-Token") or ""
            if not secrets.compare_digest(supplied, self.token):
                self._deny("bad token")
                return True
        return False

    def _send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status, payload):
        self._send_bytes(json.dumps(payload).encode(), "application/json", status)

    def _send_file(self, name, content_type):
        path = RESOURCE_DIR / name
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(404, {"success": False, "error": "not found"})
            return
        self._send_bytes(body, content_type)

    def _chunk(self, payload: bytes):
        self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
        self.wfile.flush()

    def _sse(self, name, data):
        """Write one SSE frame. The `data:` prefix is mandatory -- a bare JSON
        line is silently discarded by EventSource, so the client just goes quiet."""
        frame = json.dumps({"name": name, "data": data}).encode()
        self._chunk(b"data: " + frame + b"\n\n")

    def _end_chunks(self):
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            pass

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)

        if path == "/events":
            return self._serve_events(params.get("token", [""])[0])
        if path in ("/", "/index.html"):
            return self._send_file("index.html", "text/html; charset=utf-8")
        if path == "/background.png":
            return self._send_file("background.png", "image/png")
        self._send_json(404, {"success": False, "error": "not found"})

    do_HEAD = do_GET

    def _serve_events(self, token):
        if self._reject(token):
            return
        q = self.bus.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            # _chunk() writes chunk-framed bodies, so HTTP/1.1 has to be told.
            # Without this the response carries no framing at all and a browser
            # EventSource silently refuses it -- urllib happens to tolerate it.
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            # Resync from live state rather than replaying progress events a
            # reconnecting client missed: its percentages would be a lie.
            self._sse("stateChanged", self.api._state_dict())

            while self.bus.is_active(q):
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    self._chunk(b": heartbeat\n\n")
                    continue
                if event is None:
                    break
                self._sse(event[0], event[1])
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            pass
        finally:
            self.bus.unsubscribe(q)
            self._end_chunks()

    def do_POST(self):
        path, _, query = self.path.partition("?")
        if path != "/rpc":
            return self._send_json(404, {"success": False, "error": "not found"})
        if self._reject(urllib.parse.parse_qs(query).get("token", [""])[0]):
            return

        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._deny("expected application/json", 415)

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._deny("bad content-length", 400)
        if length <= 0 or length > 8 * 1024 * 1024:
            return self._deny("bad payload size", 413)

        try:
            call = json.loads(self.rfile.read(length))
            method = call["method"]
            args = call.get("args") or []
            if not isinstance(args, list):
                raise TypeError("args must be a list")
        except Exception as exc:
            return self._deny(f"bad request: {exc}", 400)

        # Public instance methods only.
        if not isinstance(method, str) or method.startswith("_"):
            return self._deny("unknown method", 404)
        fn = getattr(self.api, method, None)
        if not callable(fn):
            return self._deny("unknown method", 404)

        try:
            result = fn(*args)
            body = json.dumps(result)
        except Exception as exc:
            log_exception(f"rpc {method}")
            return self._send_json(500, {"success": False, "error": f"{type(exc).__name__}: {exc}"})

        self._send_bytes(body.encode(), "application/json")
class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A browser closing a tab resets every socket it held. That is not an
        # error worth a traceback in the launcher's face.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            return
        log_exception("http request")
        super().handle_error(request, client_address)
def serve():
    import secrets
    import webbrowser

    api = API()
    bus = EventBus()
    stopping = threading.Event()

    token = secrets.token_urlsafe(24)
    handler = type("LoadstoneHandler", (_Handler,), {"api": api, "bus": bus, "token": token})
    # Port 0 lets the OS pick a free port: no fixed port to collide with, and
    # nothing for a local process to guess at before the token check.
    httpd = _Server(("127.0.0.1", 0), handler)
    handler.port = httpd.server_address[1]

    def quit_app():
        stopping.set()

    api._attach(bus, quit_app)
    http_thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.5},
                                  daemon=True)
    http_thread.start()

    url = f"http://127.0.0.1:{handler.port}/#{token}"
    logging.info("UI listening on %s", url)
    try:
        webbrowser.open(url)
    except Exception:
        log_exception("open browser")

    def watchdog():
        idle = 0
        while not stopping.wait(1.0):
            if bus.client_count() or api._busy():
                idle = 0
                continue
            # Grace period first: the browser has not connected yet at boot.
            idle += 1
            if idle >= 15:
                logging.info("no UI attached and nothing running, exiting")
                quit_app()
                return

    threading.Thread(target=watchdog, daemon=True).start()
    stopping.wait()

    try:
        api.stop_all_servers()
    except Exception:
        log_exception("stop servers on shutdown")

    # shutdown() returns only once serve_forever has actually left its loop.
    # Closing the socket before that races it on Windows, which kills the
    # thread with WinError 10038 from inside select().
    httpd.shutdown()
    http_thread.join(timeout=5)
    httpd.server_close()
