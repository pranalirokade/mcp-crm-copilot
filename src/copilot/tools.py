"""Concrete CRM tools + registry factory. Every text field leaving a tool is PII-masked."""
from __future__ import annotations

from . import db
from .guardrails import mask_pii, read_only_authorizer, validate_sql
from .registry import Tool, ToolError, ToolRegistry

STATUSES = ["open", "pending", "resolved", "closed", "escalated"]
PRIORITIES = ["low", "normal", "high", "urgent"]
CATEGORIES = ["billing", "technical", "account_access", "feature_request", "cancellation", "legal", "other"]
PLANS = ["free", "basic", "pro", "enterprise"]
SQL_TABLES = {"customers", "tickets", "crm_notes"}   # approvals / audit_log are NOT queryable


def _mask_row(row) -> dict:
    return {k: (mask_pii(v)[0] if isinstance(v, str) else v) for k, v in dict(row).items()}


def search_tickets(conn, query: str, status: str | None = None, limit: int = 10):
    sql, params = "SELECT id,subject,status,priority,category FROM tickets WHERE (subject LIKE ? OR body LIKE ?)", [f"%{query}%"] * 2
    if status:
        sql, params = sql + " AND status=?", params + [status]
    return [_mask_row(r) for r in conn.execute(sql + " ORDER BY id LIMIT ?", params + [limit])]


def get_ticket(conn, ticket_id: int):
    r = conn.execute("""SELECT t.*, c.name AS customer_name, c.tier AS customer_tier, c.plan AS customer_plan
                        FROM tickets t JOIN customers c ON c.id=t.customer_id WHERE t.id=?""", (ticket_id,)).fetchone()
    if r is None:
        raise ToolError(f"ticket {ticket_id} not found")
    return _mask_row(r)


def run_sql_query(conn, sql: str):
    safe = validate_sql(sql, SQL_TABLES, max_rows=100)
    conn.set_authorizer(read_only_authorizer)
    try:
        cur = conn.execute(safe)
        rows = [_mask_row(r) for r in cur.fetchall()]
    finally:
        conn.set_authorizer(None)
    return {"sql_executed": safe, "row_count": len(rows), "rows": rows}


def update_ticket(conn, ticket_id: int, status: str | None = None, priority: str | None = None,
                  category: str | None = None, assigned_to: str | None = None):
    get_ticket(conn, ticket_id)
    changes = {k: v for k, v in dict(status=status, priority=priority, category=category,
                                     assigned_to=assigned_to).items() if v is not None}
    if not changes:
        raise ToolError("nothing to update")
    conn.execute(f"UPDATE tickets SET {', '.join(f'{k}=?' for k in changes)} WHERE id=?", [*changes.values(), ticket_id])
    conn.commit()
    return {"ticket_id": ticket_id, "updated": changes}


def update_crm_record(conn, customer_id: int, note: str):
    if conn.execute("SELECT 1 FROM customers WHERE id=?", (customer_id,)).fetchone() is None:
        raise ToolError(f"customer {customer_id} not found")
    clean, _ = mask_pii(note)   # never persist raw PII in notes
    conn.execute("INSERT INTO crm_notes(customer_id,note,author,created_at) VALUES (?,?,?,?)",
                 (customer_id, clean, "copilot", db.now()))
    conn.commit()
    return {"customer_id": customer_id, "note_saved": clean}


def update_customer_plan(conn, customer_id: int, plan: str):
    if conn.execute("SELECT 1 FROM customers WHERE id=?", (customer_id,)).fetchone() is None:
        raise ToolError(f"customer {customer_id} not found")
    conn.execute("UPDATE customers SET plan=? WHERE id=?", (plan, customer_id))
    conn.commit()
    return {"customer_id": customer_id, "plan": plan}


def escalate_to_human(conn, ticket_id: int, reason: str):
    update_ticket(conn, ticket_id, status="escalated", assigned_to="human_queue")
    clean, _ = mask_pii(reason)
    c = conn.execute("SELECT customer_id FROM tickets WHERE id=?", (ticket_id,)).fetchone()[0]
    update_crm_record(conn, c, f"Ticket {ticket_id} escalated to human: {clean}")
    return {"ticket_id": ticket_id, "escalated": True, "reason": clean}


def _schema(props, required):
    return {"type": "object", "properties": props, "required": required}


def build_registry(conn) -> ToolRegistry:
    r = ToolRegistry(conn)
    S, I = {"type": "string"}, {"type": "integer", "minimum": 1}
    for t in (
        Tool("search_tickets", "Search tickets by keyword in subject/body.", _schema(
            {"query": {"type": "string", "minLength": 2, "maxLength": 100}, "status": {"type": "string", "enum": STATUSES},
             "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, ["query"]), search_tickets),
        Tool("get_ticket", "Fetch one ticket with customer tier/plan (PII masked).", _schema({"ticket_id": I}, ["ticket_id"]), get_ticket),
        Tool("run_sql_query", "Run a read-only SELECT over customers, tickets, crm_notes (max 100 rows, PII masked).",
             _schema({"sql": {"type": "string", "minLength": 6, "maxLength": 1000}}, ["sql"]), run_sql_query),
        Tool("update_ticket", "Update ticket status/priority/category/assignee.", _schema(
            {"ticket_id": I, "status": {"type": "string", "enum": STATUSES}, "priority": {"type": "string", "enum": PRIORITIES},
             "category": {"type": "string", "enum": CATEGORIES}, "assigned_to": {"type": "string", "maxLength": 50}},
            ["ticket_id"]), update_ticket, risk="write"),
        Tool("update_crm_record", "Append a note to a customer's CRM record.", _schema(
            {"customer_id": I, "note": {"type": "string", "minLength": 3, "maxLength": 500}}, ["customer_id", "note"]),
             update_crm_record, risk="write"),
        Tool("update_customer_plan", "Change a customer's subscription plan. Requires human approval.", _schema(
            {"customer_id": I, "plan": {"type": "string", "enum": PLANS}}, ["customer_id", "plan"]),
             update_customer_plan, risk="high"),
        Tool("escalate_to_human", "Hand a ticket to the human support queue.", _schema(
            {"ticket_id": I, "reason": {"type": "string", "minLength": 3, "maxLength": 300}}, ["ticket_id", "reason"]),
             escalate_to_human, risk="write"),
    ):
        r.register(t)
    return r
