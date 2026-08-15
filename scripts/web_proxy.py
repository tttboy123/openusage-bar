"""Reverse proxy that serves the React web app and proxies /v1/* to the demo backend.

Usage:
    .build-venv/bin/python scripts/web_proxy.py --web-root web/dist --api-port 17822 --port 6020
"""

import argparse
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    import http.client as httplib
except ImportError:  # pragma: no cover
    import httplib


def _proxy(req: BaseHTTPRequestHandler, api_port: int) -> None:
    parsed = urlparse(req.path)
    target = httplib.HTTPConnection("127.0.0.1", api_port, timeout=10)
    headers = {k: v for k, v in req.headers.items() if k.lower() not in ("host", "content-length")}
    headers["Host"] = f"127.0.0.1:{api_port}"
    try:
        target.request(req.command, parsed.path + ("?" + parsed.query if parsed.query else ""), body=None, headers=headers)
        response = target.getresponse()
        req.send_response(response.status)
        for k, v in response.getheaders():
            if k.lower() not in ("transfer-encoding", "content-length", "connection"):
                req.send_header(k, v)
        body = response.read()
        req.send_header("Content-Length", str(len(body)))
        req.end_headers()
        req.wfile.write(body)
    finally:
        target.close()


def make_handler(web_root: Path, api_port: int):
    class _Handler(BaseHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            self.web_root = web_root
            super().__init__(*args, **kwargs)

        def log_message(self, format, *args):
            return

        def do_GET(self):
            if self.path.startswith("/v1/"):
                _proxy(self, api_port)
                return
            file_path = self.web_root / self.path.lstrip("/")
            if not file_path.exists() or file_path.is_dir():
                file_path = self.web_root / "index.html"
            try:
                data = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", _mime(file_path.suffix))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"not found")

        def do_POST(self):
            if not self.path.startswith("/v1/"):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"not found")
                return
            _proxy(self, api_port)

        def do_HEAD(self):
            if self.path.startswith("/v1/"):
                _proxy(self, api_port)
                return
            self.do_GET()

    return _Handler


def _mime(ext: str) -> str:
    return {
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript",
        ".css": "text/css",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".json": "application/json",
        ".woff2": "font/woff2",
    }.get(ext, "application/octet-stream")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--web-root", required=True)
    parser.add_argument("--api-port", type=int, default=17822)
    parser.add_argument("--port", type=int, default=6020)
    args = parser.parse_args()
    web_root = Path(args.web_root).resolve()
    handler = make_handler(web_root, args.api_port)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"Proxy on http://127.0.0.1:{args.port} -> API http://127.0.0.1:{args.api_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    main()
