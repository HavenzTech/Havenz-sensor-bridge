"""
The harness's client for the Havenz API.

It behaves like a careful person with a browser: signs in, keeps its token fresh, sends the
company header, and - because every rate limit in the rehearsal is left at its production value -
paces itself so it stays inside them. The limits are per signed-in account, so set-up work is
spread across the plant's administrator accounts the way a real team's would be, rather than one
account being given a raised limit.

Any 429 that does get through is counted (`Api.rate_limited`) and reported: the harness pacing
itself is deliberate, but a 429 it did not expect is information.
"""

import json
import threading
import time
import uuid
from collections import deque

from .util import http, parse_iso

# Production: 100 requests a minute per account (global) and 10 a minute for "sensitive" routes
# (photo enrolment, emergency announcements, re-syncs). Stay under both with room to spare. The
# limiter counts fixed one-minute windows; a sliding window is the stricter test, so passing it
# guarantees passing theirs.
GENERAL_PER_MINUTE = 80
SENSITIVE_PER_MINUTE = 8


class Account:
    def __init__(self, email, password, label=None):
        self.email = email
        self.password = password
        self.label = label or email
        self.token = None
        self.refresh_token = None
        self.expires_at = 0.0
        self.user_id = None
        self.calls = deque()
        self.sensitive_calls = deque()
        self.lock = threading.Lock()

    def headroom(self):
        now = time.monotonic()
        while self.calls and now - self.calls[0] > 60:
            self.calls.popleft()
        return GENERAL_PER_MINUTE - len(self.calls)

    def reserve(self, sensitive):
        """Block until this account may make one more call, then record it."""
        while True:
            with self.lock:
                now = time.monotonic()
                while self.calls and now - self.calls[0] > 60:
                    self.calls.popleft()
                while self.sensitive_calls and now - self.sensitive_calls[0] > 60:
                    self.sensitive_calls.popleft()
                wait = 0.0
                if len(self.calls) >= GENERAL_PER_MINUTE:
                    wait = max(wait, 60 - (now - self.calls[0]) + 0.05)
                if sensitive and len(self.sensitive_calls) >= SENSITIVE_PER_MINUTE:
                    wait = max(wait, 60 - (now - self.sensitive_calls[0]) + 0.05)
                if wait <= 0:
                    self.calls.append(now)
                    if sensitive:
                        self.sensitive_calls.append(now)
                    return
            time.sleep(min(wait, 2.0))

    def sensitive_headroom(self):
        now = time.monotonic()
        while self.sensitive_calls and now - self.sensitive_calls[0] > 60:
            self.sensitive_calls.popleft()
        return SENSITIVE_PER_MINUTE - len(self.sensitive_calls)


class ApiError(Exception):
    def __init__(self, status, body, method, path):
        detail = body if isinstance(body, str) else json.dumps(body)[:600]
        super().__init__(f"{method} {path} -> HTTP {status}: {detail}")
        self.status, self.body, self.method, self.path = status, body, method, path


class Api:
    def __init__(self, base, company_id=None):
        self.base = base.rstrip("/")
        self.company_id = company_id
        self.admins = []              # accounts that may do administrator work, in the plant's company
        self.rate_limited = 0
        self.calls = 0
        self._pick = threading.Lock()

    # -- sign-in --------------------------------------------------------------

    def login(self, account):
        status, body, _ = http("POST", f"{self.base}/api/auth/login",
                               body={"email": account.email, "password": account.password}, timeout=30)
        if status != 200 or not isinstance(body, dict) or not body.get("token"):
            raise ApiError(status, body, "POST", "/api/auth/login")
        account.token = body["token"]
        account.refresh_token = body.get("refreshToken")
        account.user_id = body.get("userId")
        expires = parse_iso(body.get("expiresAt"))
        account.expires_at = expires.timestamp() if expires else time.time() + 600
        account.required_actions = body.get("requiredActions") or []
        return body

    def ensure_token(self, account):
        if account.token is None or time.time() > account.expires_at - 60:
            self.login(account)
        return account.token

    def change_password(self, account, new_password):
        """An administrator-created account must replace its temporary password before anything else."""
        self.ensure_token(account)
        status, body, _ = http("POST", f"{self.base}/api/auth/change-password",
                               body={"currentPassword": account.password, "newPassword": new_password},
                               headers={"Authorization": f"Bearer {account.token}"}, timeout=30)
        if status not in (200, 204):
            raise ApiError(status, body, "POST", "/api/auth/change-password")
        account.password = new_password
        account.token = None
        self.login(account)

    # -- calls ----------------------------------------------------------------

    def pick_admin(self, sensitive=False):
        """The administrator account with the most room left in its minute."""
        with self._pick:
            key = (lambda a: a.sensitive_headroom()) if sensitive else (lambda a: a.headroom())
            return max(self.admins, key=key)

    def call(self, method, path, body=None, *, account=None, company=True, headers=None,
             expect=None, timeout=60, raw=None, content_type=None, sensitive=False):
        """
        One API call as `account` (default: whichever administrator has room). Returns
        (status, body). With `expect`, a status outside it raises ApiError - used where the call
        is set-up and anything else means the rehearsal cannot continue. Without it the status is
        simply returned, because a refusal is often exactly what a scenario is checking for.
        """
        account = account or self.pick_admin(sensitive)
        for attempt in range(6):
            account.reserve(sensitive)
            token = self.ensure_token(account)
            hdrs = {"Authorization": f"Bearer {token}"}
            if company and self.company_id:
                hdrs["X-Company-Id"] = company if isinstance(company, str) else self.company_id
            if content_type:
                hdrs["Content-Type"] = content_type
            hdrs.update(headers or {})
            status, parsed, rh = http(method, f"{self.base}{path}", body=body, headers=hdrs,
                                      timeout=timeout, raw=raw)
            self.calls += 1
            if status == 401 and attempt == 0:
                account.token = None           # expired between the check and the call
                continue
            if status == 429:
                self.rate_limited += 1
                retry_after = rh.get("Retry-After") or rh.get("retry-after") or "5"
                try:
                    time.sleep(min(float(retry_after), 60) + 0.5)
                except ValueError:
                    time.sleep(5)
                continue
            break
        if expect is not None:
            allowed = expect if isinstance(expect, (tuple, list, set)) else (expect,)
            if status not in allowed:
                raise ApiError(status, parsed, method, path)
        return status, parsed

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body=body, **kw)

    def put(self, path, body=None, **kw):
        return self.call("PUT", path, body=body, **kw)

    def delete(self, path, **kw):
        return self.call("DELETE", path, **kw)

    def upload_photo(self, path, jpeg_bytes, *, account=None, filename="face.jpg", expect=None):
        """multipart/form-data with a single `photo` part - the way the web and phone apps send it."""
        boundary = "----rehearsal" + uuid.uuid4().hex
        body = (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
                f"Content-Type: image/jpeg\r\n\r\n").encode() + jpeg_bytes + f"\r\n--{boundary}--\r\n".encode()
        return self.call("POST", path, raw=body, account=account, sensitive=True, expect=expect,
                         content_type=f"multipart/form-data; boundary={boundary}", timeout=120)
