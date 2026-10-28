"""Local web dashboard: a small stdlib HTTP server over an AgentRunner.

Security model. The dashboard can sell your positions, so:
  * It binds to 127.0.0.1 by default. Binding anywhere else requires a password
    (HTTP basic auth, from MEMECOIN_UI_PASSWORD).
  * Every /api request must carry a per-process random token, which is only
    delivered inside the served page. Other websites can't read the page
    (same-origin policy), so they can't forge requests, and the custom header
    forces a CORS preflight that this server never approves.
  * The Host header must name this server, which defeats DNS-rebinding attacks.
  * The page is served with a strict CSP and frame-ancestors 'none'
    (no clickjacking).
"""

from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import logging
import secrets
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .runner import AgentRunner, LogBuffer, token_view

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {"app.js": "text/javascript; charset=utf-8", "app.css": "text/css; charset=utf-8"}
RANGES = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7)}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
MAX_BODY = 64 * 1024


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, runner: AgentRunner, logs: LogBuffer, host: str = "127.0.0.1", port: int = 8787,
                 password: str | None = None, config_dict: dict[str, Any] | None = None) -> None:
        if not is_loopback(host) and not password:
            raise ValueError(f"Refusing to serve the dashboard on {host} without a password "
                             "(set MEMECOIN_UI_PASSWORD), since it can sell your positions")
        self.runner = runner
        self.logs = logs
        self.password = password
        self.token = secrets.token_urlsafe(24)
        self.config_dict = config_dict or {}
        self.bind_host = host
        super().__init__((host, port), DashboardHandler)
        self.port = self.server_address[1]

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.bind_host in ("0.0.0.0", "::") else self.bind_host
        return f"http://{host}:{self.port}/"

    def allowed_hosts(self) -> set[str]:
        names = {"127.0.0.1", "localhost", "[::1]", self.bind_host}
        return {f"{n}:{self.port}" for n in names}


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = "memecoin-agent"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the access log out of the agent log
        log.debug("%s %s", self.address_string(), fmt % args)

    # -- helpers --------------------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: Any, status: int = 200) -> None:
        self._send(status, json.dumps(data, default=str).encode(), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json({"ok": False, "error": message}, status)

    def _authorized(self, api: bool) -> bool:
        srv = self.server
        host = self.headers.get("Host", "")
        if srv.password:
            auth = self.headers.get("Authorization", "")
            ok = False
            if auth.startswith("Basic "):
                try:
                    _, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                    ok = hmac.compare_digest(pw, srv.password)
                except (ValueError, UnicodeDecodeError):
                    ok = False
            if not ok:
                self._send(401, b"Authentication required", "text/plain",
                           {"WWW-Authenticate": 'Basic realm="memecoin-agent"'})
                return False
        elif host not in srv.allowed_hosts():
            self._error(HTTPStatus.FORBIDDEN, "Unexpected Host header")
            return False
        if api:
            token = self.headers.get("X-UI-Token") or parse_qs(urlparse(self.path).query).get("token", [""])[0]
            if not hmac.compare_digest(token, srv.token):
                self._error(HTTPStatus.FORBIDDEN, "Missing or invalid UI token; reload the page")
                return False
        return True

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("Request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        return data

    # -- routes ---------------------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        url = urlparse(self.path)
        path = url.path
        api = path.startswith("/api/")
        if not self._authorized(api):
            return
        q = parse_qs(url.query)
        srv, runner = self.server, self.server.runner
        if path in ("/", "/index.html"):
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("__UI_TOKEN__", srv.token)
            self._send(200, html.encode(), "text/html; charset=utf-8",
                       {"Content-Security-Policy": CSP, "X-Frame-Options": "DENY"})
        elif path.startswith("/static/") and path[len("/static/"):] in STATIC_FILES:
            name = path[len("/static/"):]
            self._send(200, (STATIC_DIR / name).read_bytes(), STATIC_FILES[name])
        elif path == "/api/view":
            self._json(runner.view)
        elif path == "/api/equity":
            rng = q.get("range", ["24h"])[0]
            since = None
            if rng in RANGES:
                now = datetime.fromisoformat(runner.view["now"]) if runner.view.get("now") else None
                since = now - RANGES[rng] if now else None
            try:  # the chart resamples on its side; candles need enough samples for real highs and lows
                max_points = min(5000, max(50, int(q.get("points", ["600"])[0])))
            except ValueError:
                max_points = 600
            self._json({"range": rng, "points": runner.equity.series(since, max_points)})
        elif path == "/api/log":
            try:
                after = int(q.get("after", ["0"])[0])
            except ValueError:
                after = 0
            self._json({"entries": srv.logs.since(after)})
        elif path == "/api/config":
            self._json(srv.config_dict)
        elif path == "/api/lookup":
            query = q.get("q", [""])[0].strip()[:120]
            try:  # read-only DexScreener lookups: served on this thread, the agent never waits for them
                found = runner.agent.lookup(query) if query else []
            except Exception as exc:
                self._error(HTTPStatus.BAD_GATEWAY, f"Search failed: {exc}")
                return
            self._json({"results": [token_view(sn, runner.agent.clock()) for sn in found]})
        elif path == "/api/backtest":
            self._json(runner.backtest.state)
        elif path == "/api/trades.csv":
            # One journal for all time; ?range= picks session, 1h-24h or all (the default).
            from ..export import select

            try:
                part = select(runner.agent.cfg.agent.trades_csv, q.get("range", ["all"])[0],
                              runner.agent.clock(), runner.started_at)
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._send(200, part.to_csv(), "text/csv; charset=utf-8",
                       {"Content-Disposition": f'attachment; filename="{part.filename}"'})
        else:
            self._error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._authorized(api=True):
            return
        runner = self.server.runner
        try:
            body = self._body()
            if path == "/api/control":
                action = str(body.get("action", ""))
                args = {k: body.get(k) for k in ("mint", "fraction", "managed", "chain", "address", "usd") if k in body}
                result = runner.submit(action, **args)
                self._json({"ok": True, **(result or {})})
            elif path == "/api/backtest":
                overrides = [str(o).strip() for o in body.get("overrides") or [] if str(o).strip()]
                directory = str(body.get("dir") or runner.agent.cfg.recorder.dir)
                state = runner.backtest.start(runner.agent.cfg, directory, overrides, bool(body.get("assume_safe")))
                self._json({"ok": True, **state})
            else:
                self._error(HTTPStatus.NOT_FOUND, "Not found")
        except (ValueError, RuntimeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except (TimeoutError, FutureTimeout):  # distinct classes before Python 3.11
            self._error(HTTPStatus.GATEWAY_TIMEOUT, "The agent is busy; the action is still queued")
        except Exception as exc:  # never leak a stack trace to the page, but log it
            log.exception("Dashboard request failed")
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")
