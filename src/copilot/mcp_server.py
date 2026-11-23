"""Expose the validated tool registry over the Model Context Protocol (stdio).

    COPILOT_DB=data/crm.db python -m copilot.mcp_server

Claude Desktop / any MCP client config:
    {"mcpServers": {"crm-copilot": {"command": "python", "args": ["-m", "copilot.mcp_server"],
                                      "env": {"COPILOT_DB": "/abs/path/data/crm.db", "PYTHONPATH": "/abs/path/src"}}}}

High-risk tools never execute from here: they queue an approval that a human resolves with
`python -m copilot.cli approve <id>`.
"""
import json
import os

from mcp.server.fastmcp import FastMCP

from . import db
from .tools import build_registry

conn = db.connect(os.getenv("COPILOT_DB", "data/crm.db"))
registry = build_registry(conn)
mcp = FastMCP("crm-copilot")


def _out(result: dict) -> str:
    return json.dumps(result, default=str)


@mcp.tool()
def search_tickets(query: str, status: str | None = None, limit: int = 10) -> str:
    """Search support tickets by keyword."""
    args = {"query": query, "limit": limit, **({"status": status} if status else {})}
    return _out(registry.call("search_tickets", args, actor="mcp"))


@mcp.tool()
def get_ticket(ticket_id: int) -> str:
    """Fetch a ticket with customer tier/plan. PII is masked."""
    return _out(registry.call("get_ticket", {"ticket_id": ticket_id}, actor="mcp"))


@mcp.tool()
def run_sql_query(sql: str) -> str:
    """Run a read-only SELECT over customers, tickets and crm_notes (max 100 rows)."""
    return _out(registry.call("run_sql_query", {"sql": sql}, actor="mcp"))


@mcp.tool()
def update_ticket(ticket_id: int, status: str | None = None, priority: str | None = None,
                  category: str | None = None, assigned_to: str | None = None) -> str:
    """Update a ticket's status, priority, category or assignee."""
    args = {k: v for k, v in dict(ticket_id=ticket_id, status=status, priority=priority,
                                  category=category, assigned_to=assigned_to).items() if v is not None}
    return _out(registry.call("update_ticket", args, actor="mcp"))


@mcp.tool()
def update_crm_record(customer_id: int, note: str) -> str:
    """Append a note to a customer's CRM record (PII is masked before saving)."""
    return _out(registry.call("update_crm_record", {"customer_id": customer_id, "note": note}, actor="mcp"))


@mcp.tool()
def update_customer_plan(customer_id: int, plan: str) -> str:
    """Request a plan change. Queued for HUMAN APPROVAL - it does not execute immediately."""
    return _out(registry.call("update_customer_plan", {"customer_id": customer_id, "plan": plan}, actor="mcp"))


@mcp.tool()
def escalate_to_human(ticket_id: int, reason: str) -> str:
    """Escalate a ticket to the human support queue."""
    return _out(registry.call("escalate_to_human", {"ticket_id": ticket_id, "reason": reason}, actor="mcp"))


if __name__ == "__main__":
    mcp.run()
