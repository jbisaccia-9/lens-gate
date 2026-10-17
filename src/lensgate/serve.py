"""The chat panel: Metabase on the left, the gated assistant on the right.

Stdlib only. Two modes:

* offline (default) - the LocalBackend; sign in by pasting a dev-IdP token
  from `python -m lensgate token data <email>`.
* live (`--metabase URL`) - the person signs in to Metabase with their own
  email and password; the panel keeps *their* session and nothing else. With
  LENSGATE_ENTRA_TENANT_ID set, the ID token is verified against Entra instead
  of the dev IdP.

Before it binds a port, the server refuses to start if any admin credential is
visible in its environment. L2 grades what the broker recorded; this check is
what makes the record believable - the process has nothing else to use.
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from .backend import Credential, LocalBackend
from .broker import Broker
from .idp import DevIdP, EntraVerifier, IdentityError
from .policy import Policy
from . import providers

FORBIDDEN_ENV = ("LENSGATE_MB_ADMIN_PASSWORD", "METABASE_API_KEY", "MB_API_KEY", "LENSGATE_MB_API_KEY")


def assert_no_admin_credentials(env=os.environ) -> None:
    found = [k for k in FORBIDDEN_ENV if env.get(k)]
    if found:
        raise SystemExit(f"refusing to start: admin credential(s) in environment: {', '.join(found)}. "
                         "The broker must only ever hold the asking person's own session.")


def serve(data: Path, port: int, metabase_url: str | None = None) -> None:
    assert_no_admin_credentials()
    policy = Policy.load()
    idp = DevIdP(data, policy)
    jwks = idp.jwks()
    entra = EntraVerifier(policy) if os.environ.get("LENSGATE_ENTRA_TENANT_ID") else None
    provider = providers.get()
    out = Path("out") / "serve"
    sessions: dict[str, object] = {}  # cookie -> metabase.Client (live mode only)
    live = None
    if metabase_url:
        from .metabase import LIVE_STATE
        live = json.loads(LIVE_STATE.read_text(encoding="utf-8"))
    page = resources.files("lensgate.static").joinpath("chat.html").read_text(encoding="utf-8")
    page = page.replace("{{METABASE_URL}}", metabase_url or "").replace("{{COMPANY}}", policy.company)

    class H(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json", headers=None):
            raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def _cookie(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "lg":
                    return v
            return None

        def log_message(self, fmt, *args):  # quieter
            pass

        def do_GET(self):
            if self.path == "/":
                return self._send(200, page, "text/html; charset=utf-8")
            if self.path == "/api/dashboards":
                if live:
                    return self._send(200, {"mode": "live", "dashboards": live["dashboards"]})
                st = LocalBackend(data, policy).state
                return self._send(200, {"mode": "offline", "dashboards": st["dashboards"]})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path == "/api/login" and live:
                from .metabase import Client, MetabaseError
                try:
                    client = Client(metabase_url).login(body["email"], body["password"])
                except (MetabaseError, KeyError) as e:
                    return self._send(401, {"error": f"Metabase sign-in failed: {e}"})
                cookie = secrets.token_urlsafe(24)
                sessions[cookie] = client
                return self._send(200, {"ok": True}, headers={"Set-Cookie": f"lg={cookie}; HttpOnly; SameSite=Strict; Path=/"})
            if self.path != "/api/ask":
                return self._send(404, {"error": "not found"})
            token = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            question = (body.get("question") or "").strip()
            if not token or not question:
                return self._send(400, {"error": "need a bearer token and a question"})
            now = datetime.now(timezone.utc)
            if live:
                from .metabase import MetabaseBackend
                client = sessions.get(self._cookie() or "")
                if client is None:
                    return self._send(401, {"error": "sign in to Metabase first"})
                backend = MetabaseBackend(client, policy, dict(live))
                me = client.get("/api/user/current")["email"]
                cred = Credential("user-session", me)
            else:
                backend, cred = LocalBackend(data, policy), None
            verifier = None
            if entra:
                def verifier(t):
                    return entra.verify(t)
            broker = Broker(policy, backend, jwks, provider, identity_verifier=verifier)
            try:
                r = broker.ask(token, question, now, out, cred=cred)
            except IdentityError as e:
                return self._send(401, {"error": str(e)})
            v = r["verdict"]
            return self._send(200, {
                "answer": r["delivered"], "outcome": r["outcome"],
                "rules": {k: ("pass" if not p else "fail") for k, p in (v.results.items() if v else [])},
                "skipped": v.skipped if v else [],
                "calls": [{"tool": c["tool"], "refused": c.get("refused"),
                           "target": c.get("connection") or c.get("collection") or c.get("dashboard")}
                          for c in r["record"]["calls"]],
                "record": str(r["dir"]),
            })

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    mode = f"live against {metabase_url}" if metabase_url else "offline"
    print(f"lens-gate chat panel ({mode}, provider={provider.name}) on http://127.0.0.1:{port}")
    srv.serve_forever()
