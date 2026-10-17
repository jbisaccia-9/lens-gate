"""The broker: the only path from the chat panel to Metabase.

It holds exactly one kind of credential - the asking person's own session -
and every tool the model can call goes through the same per-call checks the
gate applies afterwards. A refused call never runs; the model is told why and
can carry on. When the model is done, the whole turn is written to disk and
graded; only a passing turn's answer is shown. A failing one is replaced by a
refusal naming the rule, and the record stays for review.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from . import gate
from .backend import Credential
from .idp import Identity, IdentityError, verify

MAX_ROWS_TO_MODEL = 200


class Toolkit:
    """The four things a model may do. Each is checked before it runs."""

    def __init__(self, policy, backend, identity: Identity, cred: Credential):
        self.policy, self.backend, self.identity, self.cred = policy, backend, identity, cred
        self.calls: list[dict] = []
        self.saved: set[str] = set()

    def list_tables(self) -> dict:
        roles = list(self.identity.roles)
        granted = self.policy.connections_for(roles)
        curate = self.policy.curate_for(roles)
        return {
            "connections": {c: t for c, t in self.backend.describe(self.cred).items() if c in granted},
            "can_save_to": sorted(curate),
            "dashboards_you_can_edit": sorted(d for d in self.backend.dashboards(self.cred)
                                              if self.backend.dashboard_collection(d) in curate),
        }

    def run_sql(self, connection: str, sql: str) -> dict:
        call = {"tool": "run_sql", "connection": connection, "sql": sql}
        probs = gate.read_problems(self.policy, self.identity, connection, sql)
        if probs:
            call["refused"] = "L3: " + "; ".join(probs)
            self.calls.append(call)
            return {"refused": call["refused"]}
        try:
            call["result"] = self.backend.run_native(self.cred, connection, sql)
        except Exception as e:  # noqa: BLE001 - surface the database's own error to the model
            call["error"] = str(e)
            call["refused"] = f"error: {e}"
            self.calls.append(call)
            return {"error": str(e)}
        self.calls.append(call)
        res = call["result"]
        return {"columns": res["columns"], "rows": res["rows"][:MAX_ROWS_TO_MODEL],
                "row_count": len(res["rows"]), "truncated": len(res["rows"]) > MAX_ROWS_TO_MODEL}

    def _write(self, call: dict, do) -> dict:
        probs = gate.write_problems(self.policy, self.identity, call, self.backend, self.saved)
        flat = [f"{r}: {p}" for r in ("L3", "L4") for p in probs[r]]
        if flat:
            call["refused"] = "; ".join(flat)
            self.calls.append(call)
            return {"refused": call["refused"]}
        try:
            out = do()
        except Exception as e:  # noqa: BLE001
            call["refused"] = f"error: {e}"
            self.calls.append(call)
            return {"error": str(e)}
        self.calls.append(call)
        return {"ok": True, **({"detail": out} if isinstance(out, dict) else {})}

    def save_question(self, collection: str, name: str, connection: str, sql: str, display: str = "table") -> dict:
        call = {"tool": "save_question", "collection": collection, "name": name,
                "connection": connection, "sql": sql, "display": display}
        r = self._write(call, lambda: self.backend.save_question(self.cred, collection, name, connection, sql, display))
        if r.get("ok"):
            self.saved.add(name)
        return r

    def add_to_dashboard(self, dashboard: str, card: str) -> dict:
        call = {"tool": "add_to_dashboard", "dashboard": dashboard, "card": card}
        return self._write(call, lambda: self.backend.add_to_dashboard(self.cred, dashboard, card))


def refusal_text(verdict: gate.Verdict) -> str:
    rules = ", ".join(f"{r} {gate.NAMES[r]}" for r in verdict.failed) or "identity"
    return (f"I can't give you that answer: it didn't pass the access checks ({rules}). "
            "Nothing was shown from it. If you think you should have access, ask an admin to review your groups.")


class Broker:
    def __init__(self, policy, backend, jwks: dict, provider, identity_verifier=None):
        self.policy, self.backend, self.jwks, self.provider = policy, backend, jwks, provider
        self.identity_verifier = identity_verifier  # live: EntraVerifier.verify; offline: dev JWKS

    def ask(self, token: str, question: str, asked_at: datetime, out: Path, cred: Credential | None = None) -> dict:
        turn_id = f"{asked_at.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid5(uuid.NAMESPACE_URL, token + question).hex[:8]}"
        record = {"turn_id": turn_id, "asked_at": asked_at.isoformat().replace("+00:00", "Z"),
                  "question": question, "token": token, "credential": None, "calls": [], "answer": "",
                  "provider": getattr(self.provider, "name", "unknown")}
        try:
            ident = self.identity_verifier(token) if self.identity_verifier else verify(token, self.jwks, self.policy, asked_at)
        except IdentityError as e:
            record["answer"] = f"Sign-in didn't verify ({e}). Nothing was run."
            return self._finish(record, out, preflight="L1")
        cred = cred or Credential("user-session", ident.email)
        record["credential"] = cred.to_json()
        drift = gate.sync_problems(self.policy, ident, self.backend)
        if drift:
            record["answer"] = ("Your Metabase access doesn't match your directory groups yet, so nothing was run. "
                                "An admin needs to run `lensgate sync`.")
            return self._finish(record, out, preflight="L6")
        tools = Toolkit(self.policy, self.backend, ident, cred)
        record["answer"] = self.provider.answer(question, ident, tools)
        record["calls"] = tools.calls
        return self._finish(record, out)

    def _finish(self, record: dict, out: Path, preflight: str | None = None) -> dict:
        d = out / record["turn_id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "turn.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        verdict = gate.grade_dir(d, self.backend, self.policy, self.jwks) if record["credential"] else None
        if preflight:
            delivered, outcome = record["answer"], f"refused before running ({preflight})"
        elif verdict and verdict.passed:
            refused = [c for c in record["calls"] if c.get("refused")]
            delivered = record["answer"]
            outcome = "delivered" if not refused else f"delivered, {len(refused)} call(s) refused"
        else:
            delivered, outcome = refusal_text(verdict), "withheld"
        (d / "answer.md").write_text(delivered + "\n", encoding="utf-8")
        return {"dir": d, "record": record, "verdict": verdict, "delivered": delivered, "outcome": outcome}
