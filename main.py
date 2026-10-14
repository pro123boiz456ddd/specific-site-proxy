"""Entry point for the Specific-Site Reverse Proxy Relay (Python FastAPI + stdlib fallback)."""

import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from middleware.headers import decode_subpath, encode_subpath
from routes.proxy import (
    forward_with_httpx,
    forward_with_urllib,
    get_target_registry,
    log_event,
    resolve_target,
)

BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"


def load_dotenv_file(env_path: Path) -> None:
    """Load key=value pairs from .env if present without requiring external packages."""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, val = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip())


load_dotenv_file(BASE_DIR / ".env")
if not os.getenv("RENDER"):
    load_dotenv_file(BASE_DIR / ".env.example")

DEFAULT_HOST = "0.0.0.0" if os.getenv("RENDER") else "127.0.0.1"
HOST = os.getenv("HOST", DEFAULT_HOST)
PORT = int(os.getenv("PORT", "8080"))


def build_health_payload() -> dict:
    """Return structured health-check metadata."""
    registry = get_target_registry()
    return {
        "status": "ok",
        "service": "specific-site-proxy",
        "mode": "preset-target-relay",
        "arbitrary_url_unblocking": False,
        "bind": f"{HOST}:{PORT}",
        "targets": {
            key: {
                "short_code": val["short_code"],
                "launch_path": val["launch_path"],
                "encoded_root": val["encoded_root"],
            }
            for key, val in registry.items()
        },
    }


def create_fastapi_app():
    """Create the FastAPI application using async httpx streaming."""
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(
        title="Specific-Site Proxy Relay",
        description="Opens preset websites (Instagram, YouTube, TikTok) via keyword-free alias routes without arbitrary URL unblocking.",
        version="1.0.0",
    )

    @app.get("/health")
    async def health_check():
        return JSONResponse(build_health_payload())

    @app.get("/api/targets")
    async def list_targets():
        registry = get_target_registry()
        return JSONResponse({
            "mode": "specific-website-relay",
            "targets": list(registry.values()),
        })

    @app.api_route("/unblock", methods=["GET", "POST"])
    @app.api_route("/proxy", methods=["GET", "POST"])
    async def block_arbitrary_urls(request: Request):
        return JSONResponse(
            status_code=403,
            content={
                "error": "Arbitrary URL unblocking is disabled",
                "message": "Use the preset service routes (/s/alpha, /s/beta, /s/gamma) to open the specific website directly.",
            },
        )

    @app.api_route("/r/{alias}/{encoded_token:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def relay_encoded_path(alias: str, encoded_token: str, request: Request):
        decoded_subpath = decode_subpath(encoded_token)
        parsed = urlparse(decoded_subpath)
        query_str = request.url.query
        if parsed.query:
            query_str = f"{parsed.query}&{query_str}" if query_str else parsed.query
        body = await request.body()
        return await forward_with_httpx(
            method=request.method,
            target_key=alias,
            subpath=parsed.path or "/",
            query_string=query_str,
            headers=dict(request.headers),
            body=body,
        )

    @app.api_route("/s/{alias}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    @app.api_route("/s/{alias}/{subpath:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def relay_specific_site(alias: str, request: Request, subpath: str = ""):
        body = await request.body()
        return await forward_with_httpx(
            method=request.method,
            target_key=alias,
            subpath=f"/{subpath}" if subpath else "/",
            query_string=request.url.query,
            headers=dict(request.headers),
            body=body,
        )

    @app.get("/")
    async def serve_index():
        return FileResponse(PUBLIC_DIR / "index.html")

    if PUBLIC_DIR.exists():
        app.mount("/assets", StaticFiles(directory=str(PUBLIC_DIR)), name="assets")

    return app


try:
    app = create_fastapi_app()
except ImportError:
    app = None


class StdlibProxyHandler(BaseHTTPRequestHandler):
    """Zero-dependency HTTP handler exposing the exact same routes when FastAPI is unavailable."""

    def log_message(self, fmt, *args):
        log_event("http_access", client=self.address_string(), detail=fmt % args)

    def _send_json(self, status: int, data: dict) -> None:
        raw = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", f"http://{HOST}:{PORT}")
        self.end_headers()
        self.wfile.write(raw)

    def _serve_static(self, rel_path: str) -> bool:
        clean = rel_path.lstrip("/") or "index.html"
        if clean.startswith("assets/"):
            clean = clean[len("assets/"):]
        candidate = (PUBLIC_DIR / clean).resolve()
        if not str(candidate).startswith(str(PUBLIC_DIR)) or not candidate.is_file():
            return False
        content = candidate.read_bytes()
        mime_type, _ = mimetypes.guess_type(str(candidate))
        self.send_response(200)
        self.send_header("Content-Type", mime_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)
        return True

    def _handle_request(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            self._send_json(200, build_health_payload())
            return

        if path == "/api/targets":
            registry = get_target_registry()
            self._send_json(200, {
                "mode": "specific-website-relay",
                "targets": list(registry.values()),
            })
            return

        if path in ("/unblock", "/proxy"):
            self._send_json(403, {
                "error": "Arbitrary URL unblocking is disabled",
                "message": "Use the preset service routes (/s/alpha, /s/beta, /s/gamma) to open the specific website directly.",
            })
            return

        if path.startswith("/r/"):
            parts = path[len("/r/"):].split("/", 1)
            alias = parts[0]
            token = parts[1] if len(parts) > 1 else ""
            decoded = decode_subpath(token)
            dec_parsed = urlparse(decoded)
            query_str = parsed.query
            if dec_parsed.query:
                query_str = f"{dec_parsed.query}&{query_str}" if query_str else dec_parsed.query
            self._proxy_to_target(method, alias, dec_parsed.path or "/", query_str)
            return

        if path.startswith("/s/"):
            parts = path[len("/s/"):].split("/", 1)
            alias = parts[0]
            subpath = f"/{parts[1]}" if len(parts) > 1 and parts[1] else "/"
            self._proxy_to_target(method, alias, subpath, parsed.query)
            return

        if self._serve_static(path):
            return

        self._send_json(404, {"error": "Route not found", "path": path})

    def _proxy_to_target(self, method: str, alias: str, subpath: str, query_str: str) -> None:
        content_len = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(content_len) if content_len > 0 else b""
        status, resp_headers, resp_body = forward_with_urllib(
            method=method,
            target_key=alias,
            subpath=subpath,
            query_string=query_str,
            headers=dict(self.headers.items()),
            body=body,
        )
        self.send_response(status)
        for k, v in resp_headers.items():
            if k.lower() != "content-length":
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(resp_body)

    def do_GET(self):
        self._handle_request("GET")

    def do_POST(self):
        self._handle_request("POST")

    def do_PUT(self):
        self._handle_request("PUT")

    def do_DELETE(self):
        self._handle_request("DELETE")

    def do_HEAD(self):
        self._handle_request("HEAD")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", f"http://{HOST}:{PORT}")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With, X-Proxy-Token")
        self.end_headers()


if __name__ == "__main__":
    try:
        import uvicorn

        if app is not None:
            log_event("server_starting", engine="fastapi+uvicorn", host=HOST, port=PORT)
            uvicorn.run(app, host=HOST, port=PORT)
        else:
            raise ImportError("FastAPI app not initialized")
    except ImportError:
        log_event("server_starting", engine="stdlib_http", host=HOST, port=PORT)
        server = ThreadingHTTPServer((HOST, PORT), StdlibProxyHandler)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.server_close()
