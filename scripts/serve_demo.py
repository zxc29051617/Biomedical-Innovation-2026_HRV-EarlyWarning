#!/usr/bin/env python3
"""Serve the public demo assets on loopback; patient artifacts are never served."""

import argparse
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit


WEB_ROOT = Path(__file__).resolve().parents[1] / "web"
PUBLIC_ASSETS = frozenset({"index.html", "styles.css", "app.js", "demo-data.js", "research-evidence.js", "favicon.svg"})


def resolve_public_asset(root, request_path):
    """Resolve an exact public filename; deny traversal, symlinks and listings."""
    root = Path(root).absolute()
    parsed = urlsplit(request_path)
    if parsed.scheme or parsed.netloc:
        raise ValueError("absolute_request_uri_forbidden")
    decoded = unquote(parsed.path, errors="strict")
    name = "index.html" if decoded == "/" else decoded.removeprefix("/")
    if name not in PUBLIC_ASSETS or decoded != ("/" if decoded == "/" else "/" + name):
        raise ValueError("asset_not_public")
    target = root / name
    if any(path.is_symlink() for path in (target, root, *root.parents)):
        raise ValueError("asset_symlink_forbidden")
    if not target.resolve().is_relative_to(root.resolve()) or not target.is_file():
        raise ValueError("asset_unavailable")
    return target


class DemoHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, root=WEB_ROOT, **kwargs):
        self.root = root
        super().__init__(*args, **kwargs)

    def _serve(self, include_body):
        # Binding loopback is reinforced by refusing DNS-rebinding Host headers.
        host = self.headers.get("Host", "").split(":", 1)[0].lower()
        if host not in {"127.0.0.1", "localhost"}:
            self.send_error(403, "Loopback Host required")
            return
        try:
            path = resolve_public_asset(self.root, self.path)
            data = path.read_bytes()
        except (ValueError, OSError, UnicodeError):
            self.send_error(404, "Public asset not found")
            return
        self.send_response(200)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") or content_type.endswith("javascript") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        if include_body:
            self.wfile.write(data)

    def do_GET(self):
        self._serve(True)

    def do_HEAD(self):
        self._serve(False)

    def log_message(self, format, *args):
        # Paths in unsolicited requests can contain sensitive local filenames.
        return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    try:
        resolve_public_asset(WEB_ROOT, "/")
        with ThreadingHTTPServer(("127.0.0.1", args.port), partial(DemoHandler, root=WEB_ROOT)) as server:
            print(f"Local research demo: http://127.0.0.1:{args.port}/", flush=True)
            print("Only public web assets are served. Import private replay JSON manually in the browser.", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as error:
        print(f"Cannot start local demo ({type(error).__name__}); try another port or open web/index.html directly.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
