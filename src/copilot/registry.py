"""Validated function-calling tool registry with approval gating and an audit trail."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from . import db
from .guardrails import mask_pii


class ToolValidationError(ValueError):
    pass


class ToolError(RuntimeError):
    """Expected business failure (e.g. ticket not found)."""


_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool}


def validate_args(schema: dict, args: Any) -> dict:
    """Validate against a JSON-Schema subset: type, required, enum, min/max, min/maxLength, pattern."""
    if not isinstance(args, dict):
        raise ToolValidationError("arguments must be an object")
    props = schema.get("properties", {})
    for k in schema.get("required", []):
        if k not in args:
            raise ToolValidationError(f"missing required argument '{k}'")
    for k, v in args.items():
        if k not in props:
            raise ToolValidationError(f"unexpected argument '{k}'")
        spec, t = props[k], props[k]["type"]
        if isinstance(v, bool) and t != "boolean" or not isinstance(v, _TYPES[t]):
            raise ToolValidationError(f"'{k}' must be of type {t}")
        if "enum" in spec and v not in spec["enum"]:
            raise ToolValidationError(f"'{k}' must be one of {spec['enum']}")
        if "minimum" in spec and v < spec["minimum"] or "maximum" in spec and v > spec["maximum"]:
            raise ToolValidationError(f"'{k}' out of range [{spec.get('minimum')}, {spec.get('maximum')}]")
        if t == "string":
            if len(v) < spec.get("minLength", 0) or len(v) > spec.get("maxLength", 10**9):
                raise ToolValidationError(f"'{k}' length out of bounds")
            if "pattern" in spec and not re.fullmatch(spec["pattern"], v):
                raise ToolValidationError(f"'{k}' does not match required pattern")
    return args


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    fn: Callable[..., Any]
    risk: str = "read"  # read | write | high  (high => needs human approval)


class ToolRegistry:
    def __init__(self, conn):
        self.conn, self.tools = conn, {}

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def specs(self) -> list[dict]:
        """OpenAI/Gemini-style function declarations."""
        return [{"name": t.name, "description": f"[{t.risk}] {t.description}", "parameters": t.schema}
                for t in self.tools.values()]

    def _audit(self, actor, tool, args, outcome):
        masked, _ = mask_pii(json.dumps(args, default=str))
        self.conn.execute("INSERT INTO audit_log(ts,actor,tool,args_json,outcome) VALUES (?,?,?,?,?)",
                          (db.now(), actor, tool, masked, outcome))
        self.conn.commit()

    def call(self, name: str, args: dict, actor: str = "agent", approved: bool = False) -> dict:
        tool = self.tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"unknown tool '{name}'"}
        try:
            args = validate_args(tool.schema, args)
        except ToolValidationError as e:
            self._audit(actor, name, args, f"rejected: {e}")
            return {"ok": False, "error": f"invalid arguments: {e}"}
        if tool.risk == "high" and not approved:
            cur = self.conn.execute(
                "INSERT INTO approvals(tool,args_json,requested_by,requested_at) VALUES (?,?,?,?)",
                (name, json.dumps(args), actor, db.now()))
            self.conn.commit()
            self._audit(actor, name, args, f"approval_required #{cur.lastrowid}")
            return {"ok": False, "status": "approval_required", "approval_id": cur.lastrowid}
        try:
            data = tool.fn(self.conn, **args)
        except (ToolError, ValueError) as e:
            self._audit(actor, name, args, f"error: {e}")
            return {"ok": False, "error": str(e)}
        self._audit(actor, name, args, "ok" + (" (approved)" if approved else ""))
        return {"ok": True, "data": data}

    # ---- human-in-the-loop
    def pending_approvals(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM approvals WHERE status='pending'")]

    def decide(self, approval_id: int, approver: str, approve: bool) -> dict:
        row = self.conn.execute("SELECT * FROM approvals WHERE id=? AND status='pending'", (approval_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": "no such pending approval"}
        status = "approved" if approve else "rejected"
        self.conn.execute("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=?",
                          (status, approver, db.now(), approval_id))
        self.conn.commit()
        if not approve:
            self._audit(approver, row["tool"], json.loads(row["args_json"]), "rejected by human")
            return {"ok": True, "status": "rejected"}
        return self.call(row["tool"], json.loads(row["args_json"]), actor=approver, approved=True)
