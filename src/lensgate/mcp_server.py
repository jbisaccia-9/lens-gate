"""Gated tools for Claude (Desktop or Code) over MCP stdio.

Metabase's built-in MCP server (v0.60+, /api/mcp) already lets Claude search,
explore and query as the connecting person. What it doesn't do is build
dashboards. This server adds that - save a question, pin it to a dashboard -
behind the same per-call checks the chat panel uses, running as the person
who started it.

One honest difference from the chat panel: here Claude writes its prose
straight to the person, so L5 (numbers) can't be enforced before they read
it. Every call is still logged as a turn and can be graded afterwards with
`check-dir`; L1-L4 and L6 are enforced live, call by call.

Environment (names only, see .env.example):
  LENSGATE_ID_TOKEN                     the person's ID token (offline: `lensgate token`)
  LENSGATE_METABASE_URL                 live mode; omit for the offline backend
  LENSGATE_MB_EMAIL, LENSGATE_MB_PASSWORD  the person's *own* Metabase login (live)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import gate
from .backend import Credential, LocalBackend
from .broker import Toolkit
from .idp import DevIdP, EntraVerifier, verify
from .policy import Policy
from .serve import assert_no_admin_credentials


def build(data: Path):
    assert_no_admin_credentials()
    policy = Policy.load()
    token = os.environ["LENSGATE_ID_TOKEN"]
    now = datetime.now(timezone.utc)
    jwks = DevIdP(data, policy).jwks()
    ident = (EntraVerifier(policy).verify(token) if os.environ.get("LENSGATE_ENTRA_TENANT_ID")
             else verify(token, jwks, policy, now))  # L1 at startup: no verified person, no server
    if os.environ.get("LENSGATE_METABASE_URL"):
        from .metabase import LIVE_STATE, Client, MetabaseBackend
        client = Client(os.environ["LENSGATE_METABASE_URL"]).login(
            os.environ["LENSGATE_MB_EMAIL"], os.environ["LENSGATE_MB_PASSWORD"])
        backend = MetabaseBackend(client, policy, json.loads(LIVE_STATE.read_text(encoding="utf-8")))
        cred = Credential("user-session", client.get("/api/user/current")["email"])
    else:
        backend, cred = LocalBackend(data, policy), Credential("user-session", ident.email)
    drift = gate.sync_problems(policy, ident, backend)
    if drift:
        raise SystemExit("refusing to start (L6): " + "; ".join(drift))
    tools = Toolkit(policy, backend, ident, cred)
    log_dir = Path("out") / "mcp" / f"{now.strftime('%Y%m%dT%H%M%S')}-{ident.email.split('@')[0]}"
    log_dir.mkdir(parents=True, exist_ok=True)

    def record():
        (log_dir / "turn.json").write_text(json.dumps({
            "turn_id": log_dir.name, "asked_at": now.isoformat().replace("+00:00", "Z"),
            "question": "(MCP session)", "token": token, "credential": cred.to_json(),
            "calls": tools.calls, "answer": "", "provider": "mcp"}, indent=2, default=str), encoding="utf-8")

    return tools, record


def main(data: Path) -> None:
    try:  # optional extra: pip install "lens-gate[mcp]"
        from mcp.server.mcpserver import MCPServer as Server  # MCP Python SDK 2.x
    except ImportError:
        from mcp.server.fastmcp import FastMCP as Server  # 1.x

    tools, record = build(data)
    app = Server("lens-gate")

    @app.tool()
    def list_tables() -> dict:
        """Connections, tables and columns you may query; collections you may save into; dashboards you may edit."""
        return tools.list_tables()

    @app.tool()
    def run_sql(connection: str, sql: str) -> dict:
        """Run one read-only SELECT on one connection, as you. Always write schema.table."""
        r = tools.run_sql(connection, sql)
        record()
        return r

    @app.tool()
    def save_question(collection: str, name: str, connection: str, sql: str, display: str = "table") -> dict:
        """Save a SQL question as a chart in a collection you may curate."""
        r = tools.save_question(collection, name, connection, sql, display)
        record()
        return r

    @app.tool()
    def add_to_dashboard(dashboard: str, card: str) -> dict:
        """Add a question saved in this session to a dashboard you may edit."""
        r = tools.add_to_dashboard(dashboard, card)
        record()
        return r

    app.run()
