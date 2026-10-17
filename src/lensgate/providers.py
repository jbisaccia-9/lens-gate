"""Who writes the SQL and the prose. The gate doesn't care which.

* `scripted` - deterministic plans for the demo questions; CI uses it, no key needed.
* `anthropic` - Claude via the Messages API with tool use.   env: ANTHROPIC_API_KEY, LENSGATE_ANTHROPIC_MODEL
* `foundry` - an Azure AI Foundry model deployment (OpenAI-compatible chat
  completions with tools).  env: AZURE_AI_FOUNDRY_ENDPOINT, AZURE_AI_FOUNDRY_API_KEY,
  AZURE_AI_FOUNDRY_DEPLOYMENT, AZURE_AI_FOUNDRY_API_VERSION

The live providers get the same four tools as the scripted one, through the
same Toolkit, so the per-call checks and the post-hoc grade are identical.
"""
from __future__ import annotations

import json
import os
from typing import Callable

TOOL_SPECS = [
    {"name": "list_tables", "description": "List the connections, tables and columns you may query, the collections you may save into, and the dashboards you may edit.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "run_sql", "description": "Run one read-only SELECT on one connection. Always write schema.table. Do arithmetic in SQL, not in prose.",
     "parameters": {"type": "object", "properties": {"connection": {"type": "string"}, "sql": {"type": "string"}},
                    "required": ["connection", "sql"]}},
    {"name": "save_question", "description": "Save a SQL question as a chart in a collection you may curate.",
     "parameters": {"type": "object", "properties": {
         "collection": {"type": "string"}, "name": {"type": "string"}, "connection": {"type": "string"},
         "sql": {"type": "string"}, "display": {"type": "string", "enum": ["table", "line", "bar", "pie", "scalar"]}},
         "required": ["collection", "name", "connection", "sql", "display"]}},
    {"name": "add_to_dashboard", "description": "Add a question you saved in this conversation to a dashboard you may edit.",
     "parameters": {"type": "object", "properties": {"dashboard": {"type": "string"}, "card": {"type": "string"}},
                    "required": ["dashboard", "card"]}},
]

SYSTEM = """You answer analytics questions for {name} ({roles}) at {company}.
Rules the system enforces whether or not you follow them:
- You can only query the connections list_tables shows you. Anything else is refused.
- Every number in your answer must be copied exactly from a query result in this conversation.
  If you need a percentage, a difference or a rounded value, compute it in SQL and quote the result.
- Write dates and numbers exactly as the results show them (thousands separators are fine).
- If a question needs data you can't see, say so plainly. Don't guess, don't estimate.
Start with list_tables."""


def _dispatch(tools, name: str, args: dict):
    fn = {"list_tables": tools.list_tables, "run_sql": tools.run_sql,
          "save_question": tools.save_question, "add_to_dashboard": tools.add_to_dashboard}.get(name)
    if fn is None:
        return {"error": f"no tool {name}"}
    try:
        return fn(**args)
    except TypeError as e:
        return {"error": f"bad arguments: {e}"}


class ScriptedProvider:
    name = "scripted"

    def __init__(self, plans: dict[str, Callable] | None = None):
        if plans is None:
            from .demo import PLANS
            plans = PLANS
        self.plans = plans

    def answer(self, question, identity, tools) -> str:
        plan = self.plans.get(question.strip())
        if plan is None:
            return ("The scripted provider only knows the demo questions. "
                    "Set LENSGATE_PROVIDER=anthropic or foundry to ask anything else.")
        return plan(tools)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, max_steps: int = 12):
        import anthropic  # optional extra: pip install "lens-gate[live]"
        self.client = anthropic.Anthropic()
        self.model = os.environ.get("LENSGATE_ANTHROPIC_MODEL", "claude-sonnet-5-5")
        self.max_steps = max_steps

    def answer(self, question, identity, tools) -> str:
        system = SYSTEM.format(name=identity.name, roles=", ".join(identity.roles), company=tools.policy.company)
        specs = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in TOOL_SPECS]
        messages = [{"role": "user", "content": question}]
        for _ in range(self.max_steps):
            r = self.client.messages.create(model=self.model, max_tokens=2048, system=system, tools=specs, messages=messages)
            messages.append({"role": "assistant", "content": r.content})
            uses = [b for b in r.content if b.type == "tool_use"]
            if not uses:
                return "".join(b.text for b in r.content if b.type == "text").strip()
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": u.id, "content": json.dumps(_dispatch(tools, u.name, u.input), default=str)}
                for u in uses]})
        return "I ran out of steps before finishing. Try a narrower question."


class FoundryProvider:
    name = "foundry"

    def __init__(self, max_steps: int = 12):
        from openai import AzureOpenAI  # optional extra: pip install "lens-gate[live]"
        self.client = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_AI_FOUNDRY_ENDPOINT"],
            api_key=os.environ["AZURE_AI_FOUNDRY_API_KEY"],
            api_version=os.environ.get("AZURE_AI_FOUNDRY_API_VERSION", "2024-10-21"),
        )
        self.deployment = os.environ["AZURE_AI_FOUNDRY_DEPLOYMENT"]
        self.max_steps = max_steps

    def answer(self, question, identity, tools) -> str:
        system = SYSTEM.format(name=identity.name, roles=", ".join(identity.roles), company=tools.policy.company)
        specs = [{"type": "function", "function": t} for t in TOOL_SPECS]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": question}]
        for _ in range(self.max_steps):
            r = self.client.chat.completions.create(model=self.deployment, messages=messages, tools=specs)
            msg = r.choices[0].message
            messages.append(msg.model_dump(exclude_none=True))
            if not msg.tool_calls:
                return (msg.content or "").strip()
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(_dispatch(tools, tc.function.name, args), default=str)})
        return "I ran out of steps before finishing. Try a narrower question."


def get(name: str | None = None):
    name = name or os.environ.get("LENSGATE_PROVIDER", "scripted")
    return {"scripted": ScriptedProvider, "anthropic": AnthropicProvider, "foundry": FoundryProvider}[name]()
