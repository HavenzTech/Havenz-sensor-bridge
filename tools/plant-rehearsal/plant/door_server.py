"""
Serves the door panel's static pages for the rehearsal.

`public/screen.html` and `public/pair.html` carry the backend's address as a literal in the page,
and that literal is the production address. Served untouched, a rehearsal panel would pair with
and listen to production. This server reads the pages from the checkout and replaces that one
address with the rehearsal backend's as it serves them; nothing on disk is changed and every other
byte is the page as committed.

Run by `rehearsal.py up`:  python -m plant.door_server --root <door>/public --port 3200 --api http://localhost:5100
"""

import argparse
import re
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PRODUCTION_BACKEND = re.compile(rb"https://havenz-backend-[A-Za-z0-9.\-]+\.run\.app")


class Handler(SimpleHTTPRequestHandler):
    api = b""

    def log_message(self, fmt, *args):
        print(self.address_string(), fmt % args, flush=True)

    def end_headers(self):
        # A panel must never run yesterday's page: no caching anywhere in the rehearsal.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("", "/"):
            self.send_response(302)
            self.send_header("Location", "/screen.html")
            self.end_headers()
            return
        if path.endswith((".html", ".js")):
            local = Path(self.translate_path(path))
            if local.is_file():
                body, count = PRODUCTION_BACKEND.subn(self.api, local.read_bytes())
                self.send_response(200)
                self.send_header("Content-Type",
                                 "text/html; charset=utf-8" if path.endswith(".html")
                                 else "application/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Rehearsal-Backend-Rewrites", str(count))
                self.end_headers()
                self.wfile.write(body)
                return
        super().do_GET()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--port", type=int, default=3200)
    ap.add_argument("--api", required=True)
    args = ap.parse_args()
    Handler.api = args.api.encode()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(Handler, directory=args.root))
    print(f"door pages from {args.root} on :{args.port}, backend address rewritten to {args.api}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
