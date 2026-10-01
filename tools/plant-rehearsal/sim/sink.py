#!/usr/bin/env python3
"""
The rehearsal's sink: everything the backend would have sent off-site lands here and stays here.

Three things in one small process:

1. MAIL. SMTP on 1026 accepts any sender and recipient, asks for no credentials, and delivers to
   nobody. The scenarios read what arrived over HTTP (below).

2. WEBHOOKS. The backend calls two outside services by HTTP (a face-enrolment check and a
   document assistant). Those addresses are pointed at /hooks/... here: seen, answered 200, kept.

3. THE DOCUMENTS BUCKET. Face photos live in Google Cloud Storage, and the backend has no setting
   that points its storage client anywhere else. Without storage every photo upload is a 500 and
   no face ever reaches a reader, which would leave nothing to rehearse. So this process also
   answers as `storage.googleapis.com` and `oauth2.googleapis.com` over TLS on 443, with a
   certificate from a private authority that is generated here at start and trusted only by the
   rehearsal's backend container. It implements the handful of calls the backend makes: token,
   resumable upload, read, delete, list, and path-style reads for signed links. Objects are held
   in memory.

Read API on 8026:

    GET    /health
    GET    /messages[?to=addr][&since=ISO-8601][&subject=text]   newest last
    DELETE /messages
    GET    /hooks
    POST   /hooks/<anything>
    GET    /gcs/objects[?prefix=text]                             what is in the bucket right now
    GET    /gcs/log                                               every storage call, newest last

Pure standard library plus the `openssl` binary for the certificates.
"""

import base64
import email
import email.policy
import gzip
import hashlib
import json
import os
import socketserver
import ssl
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

MESSAGES = []
HOOKS = []
OBJECTS = {}            # (bucket, name) -> {"data": bytes, "contentType": str, "created": iso}
UPLOADS = {}            # upload id -> {"bucket", "name", "contentType", "data": bytearray}
GCS_LOG = []
LOCK = threading.Lock()
MAX_KEPT = 5000

TRUST_DIR = os.environ.get("TRUST_DIR", "/trust")
TLS_NAMES = ["storage.googleapis.com", "oauth2.googleapis.com", "www.googleapis.com"]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------------------------
# Mail
# ---------------------------------------------------------------------------------------------

def body_text(msg):
    """The readable part of a message: plain text if there is one, else the HTML as it is."""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        return part.get_content() if part is not None else ""
    except Exception:  # noqa: BLE001 - a sink must take anything
        payload = msg.get_payload(decode=True)
        return payload.decode("utf-8", "replace") if payload else ""


class Smtp(socketserver.StreamRequestHandler):
    def reply(self, line):
        self.wfile.write((line + "\r\n").encode())

    def handle(self):
        self.reply("220 rehearsal-sink ESMTP")
        sender, rcpts = None, []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            verb = line[:4].upper()
            if verb in ("EHLO", "HELO"):
                # No STARTTLS and no AUTH offered: a client that insists on either is misconfigured
                # for a sink, and that should fail loudly rather than be papered over.
                self.reply("250-rehearsal-sink")
                self.reply("250-8BITMIME")
                self.reply("250 SMTPUTF8")
            elif verb == "MAIL":
                sender, rcpts = line.partition(":")[2].strip(), []
                self.reply("250 OK")
            elif verb == "RCPT":
                rcpts.append(line.partition(":")[2].strip().split(" ")[0].strip("<>"))
                self.reply("250 OK")
            elif verb == "DATA":
                self.reply("354 end with <CRLF>.<CRLF>")
                chunks = []
                while True:
                    data = self.rfile.readline()
                    if not data or data in (b".\r\n", b".\n"):
                        break
                    chunks.append(data[1:] if data.startswith(b"..") else data)
                raw_message = b"".join(chunks)
                msg = email.message_from_bytes(raw_message, policy=email.policy.default)
                with LOCK:
                    MESSAGES.append({
                        "id": len(MESSAGES) + 1,
                        "receivedAt": now_iso(),
                        "from": (sender or "").split(" ")[0].strip("<>"),
                        "to": rcpts,
                        "subject": str(msg.get("Subject", "")),
                        "text": body_text(msg),
                        "bytes": len(raw_message),
                    })
                    del MESSAGES[:-MAX_KEPT]
                self.reply("250 OK queued")
            elif verb == "RSET":
                sender, rcpts = None, []
                self.reply("250 OK")
            elif verb == "NOOP":
                self.reply("250 OK")
            elif verb == "QUIT":
                self.reply("221 bye")
                return
            else:
                self.reply("502 not implemented")


class ThreadedTcp(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------------------------

def send_json(handler, code, obj, extra=None):
    body = b"" if obj is None else json.dumps(obj).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=UTF-8")
    handler.send_header("Content-Length", str(len(body)))
    for k, v in (extra or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    if body:
        handler.wfile.write(body)


class Api(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/messages":
            with LOCK:
                rows = list(MESSAGES)
            if "to" in q:
                rows = [m for m in rows if q["to"].lower() in [t.lower() for t in m["to"]]]
            if "since" in q:
                rows = [m for m in rows if m["receivedAt"] >= q["since"]]
            if "subject" in q:
                rows = [m for m in rows if q["subject"].lower() in m["subject"].lower()]
            return send_json(self, 200, rows)
        if url.path == "/hooks":
            with LOCK:
                return send_json(self, 200, list(HOOKS))
        if url.path == "/gcs/objects":
            with LOCK:
                rows = [{"bucket": b, "name": n, "size": len(o["data"]), "contentType": o["contentType"],
                         "created": o["created"]} for (b, n), o in sorted(OBJECTS.items())]
            if "prefix" in q:
                rows = [r for r in rows if q["prefix"] in r["name"]]
            return send_json(self, 200, rows)
        if url.path == "/gcs/log":
            with LOCK:
                return send_json(self, 200, list(GCS_LOG))
        if url.path == "/health":
            return send_json(self, 200, {"ok": True, "messages": len(MESSAGES), "hooks": len(HOOKS),
                                         "objects": len(OBJECTS)})
        send_json(self, 404, {"error": "not found"})

    def do_DELETE(self):
        if urlparse(self.path).path == "/messages":
            with LOCK:
                MESSAGES.clear()
            return send_json(self, 200, {"ok": True})
        send_json(self, 404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length)
        with LOCK:
            HOOKS.append({"receivedAt": now_iso(), "path": self.path, "bytes": len(raw),
                          "body": raw[:2000].decode("utf-8", "replace")})
            del HOOKS[:-MAX_KEPT]
        send_json(self, 200, {})


# ---------------------------------------------------------------------------------------------
# The bucket
# ---------------------------------------------------------------------------------------------

def _crc32c_table():
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0x82F63B78 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_CRC_TABLE = _crc32c_table()


def crc32c_b64(data):
    """CRC32C (Castagnoli), base64 of the big-endian bytes - the client verifies its upload with it."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = _CRC_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return base64.b64encode(((crc ^ 0xFFFFFFFF) & 0xFFFFFFFF).to_bytes(4, "big")).decode()


def object_resource(bucket, name, obj):
    data = obj["data"]
    return {
        "kind": "storage#object",
        "id": f"{bucket}/{name}/1",
        "selfLink": f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{name}",
        "name": name,
        "bucket": bucket,
        "generation": "1",
        "metageneration": "1",
        "contentType": obj["contentType"],
        "storageClass": "STANDARD",
        "size": str(len(data)),
        "md5Hash": base64.b64encode(hashlib.md5(data).digest()).decode(),
        "crc32c": crc32c_b64(data),
        "etag": "CAE=",
        "timeCreated": obj["created"],
        "updated": obj["created"],
    }


def gcs_log(op, bucket, name, status):
    with LOCK:
        GCS_LOG.append({"at": now_iso(), "op": op, "bucket": bucket, "name": name, "status": status})
        del GCS_LOG[:-MAX_KEPT]


class Gcs(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _body(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        # Google's client libraries gzip their JSON request bodies.
        if raw and "gzip" in (self.headers.get("Content-Encoding") or "").lower():
            raw = gzip.decompress(raw)
        return raw

    def _bytes(self, code, data, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def _split(path):
        """('/storage/v1/b/<bucket>/o/<object>') -> (bucket, object or None)."""
        marker = "/b/"
        rest = path[path.index(marker) + len(marker):]
        if "/o/" in rest:
            bucket, _, name = rest.partition("/o/")
            return unquote(bucket), unquote(name)
        if rest.endswith("/o"):
            return unquote(rest[:-2]), None
        return unquote(rest), None

    def do_POST(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        raw = self._body()

        if url.path.rstrip("/").endswith("/token"):
            # Any assertion is accepted: the only caller that can reach this is the rehearsal's own
            # backend container.
            return send_json(self, 200, {"access_token": "rehearsal-token", "expires_in": 3600,
                                         "token_type": "Bearer"})

        if url.path.startswith("/upload/storage/v1/b/"):
            bucket, _ = self._split(url.path)
            try:
                meta = json.loads(raw) if raw else {}
            except ValueError:
                meta = {}
            name = meta.get("name") or q.get("name")
            if not name:
                return send_json(self, 400, {"error": {"code": 400, "message": "name is required"}})
            upload_id = uuid.uuid4().hex
            with LOCK:
                UPLOADS[upload_id] = {
                    "bucket": bucket, "name": name, "data": bytearray(),
                    "contentType": meta.get("contentType")
                    or self.headers.get("X-Upload-Content-Type") or "application/octet-stream"}
            location = (f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o"
                        f"?uploadType=resumable&upload_id={upload_id}")
            return send_json(self, 200, None, {"Location": location})

        send_json(self, 404, {"error": {"code": 404, "message": "not found"}})

    def do_PUT(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        raw = self._body()
        upload = UPLOADS.get(q.get("upload_id") or "")
        if upload is None:
            return send_json(self, 404, {"error": {"code": 404, "message": "no such upload"}})

        upload["data"].extend(raw)
        content_range = self.headers.get("Content-Range", "")
        total = content_range.rpartition("/")[2] if "/" in content_range else str(len(upload["data"]))
        finished = total != "*" and len(upload["data"]) >= int(total)
        if not finished:
            self.send_response(308)
            if upload["data"]:
                self.send_header("Range", f"bytes=0-{len(upload['data']) - 1}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        obj = {"data": bytes(upload["data"]), "contentType": upload["contentType"], "created": now_iso()}
        with LOCK:
            OBJECTS[(upload["bucket"], upload["name"])] = obj
            UPLOADS.pop(q.get("upload_id"), None)
        gcs_log("upload", upload["bucket"], upload["name"], 200)
        send_json(self, 200, object_resource(upload["bucket"], upload["name"], obj))

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path

        if path.startswith("/storage/v1/b/") or path.startswith("/download/storage/v1/b/"):
            bucket, name = self._split(path)
            if name is None:
                if path.rstrip("/").endswith("/o"):
                    prefix = q.get("prefix", "")
                    with LOCK:
                        items = [object_resource(b, n, o) for (b, n), o in sorted(OBJECTS.items())
                                 if b == bucket and n.startswith(prefix)]
                    return send_json(self, 200, {"kind": "storage#objects", "items": items})
                return send_json(self, 200, {"kind": "storage#bucket", "id": bucket, "name": bucket})
            obj = OBJECTS.get((bucket, name))
            if obj is None:
                gcs_log("read", bucket, name, 404)
                return send_json(self, 404, {"error": {"code": 404, "message": f"No such object: {bucket}/{name}"}})
            gcs_log("read", bucket, name, 200)
            if q.get("alt") == "media" or path.startswith("/download/"):
                return self._bytes(200, obj["data"], obj["contentType"])
            return send_json(self, 200, object_resource(bucket, name, obj))

        # Path-style read, which is what a signed link is: /<bucket>/<object>?X-Goog-Signature=...
        parts = path.lstrip("/").split("/", 1)
        if len(parts) == 2:
            bucket, name = unquote(parts[0]), unquote(parts[1])
            obj = OBJECTS.get((bucket, name))
            gcs_log("signed-read", bucket, name, 200 if obj else 404)
            if obj is not None:
                return self._bytes(200, obj["data"], obj["contentType"])
        send_json(self, 404, {"error": {"code": 404, "message": "not found"}})

    def do_DELETE(self):
        url = urlparse(self.path)
        if url.path.startswith("/storage/v1/b/"):
            bucket, name = self._split(url.path)
            with LOCK:
                existed = OBJECTS.pop((bucket, name), None) is not None
            gcs_log("delete", bucket, name, 204 if existed else 404)
            if existed:
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return send_json(self, 404, {"error": {"code": 404, "message": "No such object"}})
        send_json(self, 404, {"error": {"code": 404, "message": "not found"}})


def make_trust():
    """
    A private certificate authority, a server certificate for the Google host names, and a
    service-account file with its own key - generated here, kept in a volume only the rehearsal's
    backend container mounts. Regenerated whenever that volume is new.
    """
    os.makedirs(TRUST_DIR, exist_ok=True)
    paths = {n: os.path.join(TRUST_DIR, n) for n in
             ("ca.key", "ca.pem", "tls.key", "tls.csr", "tls.pem", "san.cnf", "sa.key", "sa.json")}
    if all(os.path.exists(paths[n]) for n in ("ca.pem", "tls.key", "tls.pem", "sa.json")):
        return paths

    def openssl(*args):
        subprocess.run(["openssl", *args], check=True, capture_output=True)

    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", paths["ca.key"],
            "-out", paths["ca.pem"], "-days", "90", "-subj", "/CN=Havenz plant rehearsal (local only)")
    with open(paths["san.cnf"], "w") as f:
        f.write("subjectAltName=" + ",".join("DNS:" + n for n in TLS_NAMES) + "\n"
                "extendedKeyUsage=serverAuth\n")
    openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", paths["tls.key"],
            "-out", paths["tls.csr"], "-subj", "/CN=storage.googleapis.com")
    openssl("x509", "-req", "-in", paths["tls.csr"], "-CA", paths["ca.pem"], "-CAkey", paths["ca.key"],
            "-CAcreateserial", "-out", paths["tls.pem"], "-days", "90", "-extfile", paths["san.cnf"])
    openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", paths["sa.key"])
    with open(paths["sa.key"]) as f:
        private_key = f.read()
    with open(paths["sa.json"], "w") as f:
        json.dump({
            "type": "service_account",
            "project_id": "havenz-rehearsal",
            "private_key_id": "rehearsal",
            "private_key": private_key,
            "client_email": "rehearsal@havenz-rehearsal.iam.gserviceaccount.com",
            "client_id": "100000000000000000000",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }, f, indent=2)
    for name in ("ca.pem", "sa.json"):
        os.chmod(paths[name], 0o644)
    return paths


def main():
    smtp = ThreadedTcp(("0.0.0.0", 1026), Smtp)
    threading.Thread(target=smtp.serve_forever, daemon=True).start()

    paths = make_trust()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(paths["tls.pem"], paths["tls.key"])
    gcs = ThreadingHTTPServer(("0.0.0.0", 443), Gcs)
    gcs.socket = context.wrap_socket(gcs.socket, server_side=True)
    threading.Thread(target=gcs.serve_forever, daemon=True).start()

    api = ThreadingHTTPServer(("0.0.0.0", 8026), Api)
    print("sink: SMTP :1026, read API :8026, bucket stand-in :443", flush=True)
    try:
        api.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
