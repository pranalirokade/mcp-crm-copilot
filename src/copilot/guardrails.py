"""Guardrails: PII masking, read-only SQL validation, prompt-injection screening."""
from __future__ import annotations

import re
import sqlite3

# ---------------------------------------------------------------- PII masking
def luhn_ok(digits: str) -> bool:
    nums = [int(d) for d in digits][::-1]
    total = sum(nums[0::2]) + sum(sum(divmod(2 * d, 10)) for d in nums[1::2])
    return total % 10 == 0


CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
AADHAAR = re.compile(r"(?<!\d)\d{4}\s\d{4}\s\d{4}(?!\d)")
SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_INTL = re.compile(r"(?<![\w+])\+\d{1,3}[ -]?\d{4,5}[ -]?\d{4,6}(?!\d)")
PHONE_10 = re.compile(r"(?<!\d)(?:\(\d{3}\)|\d{3})[ -]?\d{3}[ -]?\d{4}(?!\d)")


def mask_pii(text: str) -> tuple[str, dict]:
    """Return (masked_text, {kind: count}). Cards are Luhn-validated to avoid false positives."""
    found: dict[str, int] = {}

    def bump(kind):
        found[kind] = found.get(kind, 0) + 1

    def card_sub(m):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            bump("card"); return "[CARD]"
        return m.group()

    def simple(pattern, label, kind):
        def _sub(m):
            bump(kind); return label
        return lambda s: pattern.sub(_sub, s)

    text = CARD.sub(card_sub, text)
    for fn in (simple(AADHAAR, "[ID_NUMBER]", "national_id"), simple(SSN, "[ID_NUMBER]", "national_id"),
               simple(EMAIL, "[EMAIL]", "email"), simple(PHONE_INTL, "[PHONE]", "phone"),
               simple(PHONE_10, "[PHONE]", "phone")):
        text = fn(text)
    return text, found


# ---------------------------------------------------------------- SQL safety
class SQLValidationError(ValueError):
    pass


FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|replace|vacuum|reindex|truncate|grant)\b", re.I)


def validate_sql(sql: str, allowed_tables: set[str], max_rows: int = 100) -> str:
    """Accept a single read-only SELECT over allow-listed tables; enforce a row limit."""
    s = sql.strip().rstrip(";").strip()
    if not s:
        raise SQLValidationError("empty query")
    if ";" in s:
        raise SQLValidationError("multiple statements are not allowed")
    if "--" in s or "/*" in s:
        raise SQLValidationError("SQL comments are not allowed")
    if not re.match(r"(?is)^select\b", s):
        raise SQLValidationError("only SELECT statements are allowed")
    if FORBIDDEN.search(s):
        raise SQLValidationError("query contains a forbidden keyword")
    tables = re.findall(r"(?i)\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", s)
    if not tables:
        raise SQLValidationError("query must read from a table")
    bad = sorted({t for t in tables if t.lower() not in allowed_tables})
    if bad:
        raise SQLValidationError(f"table(s) not allowed: {bad}")
    m = re.search(r"(?i)\blimit\s+(\d+)\s*$", s)
    if m:
        return s if int(m.group(1)) <= max_rows else s[: m.start()] + f"LIMIT {max_rows}"
    return f"{s} LIMIT {max_rows}"


def read_only_authorizer(action, *_):
    """SQLite authorizer: defence in depth even if validation were bypassed."""
    ok = (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION)
    return sqlite3.SQLITE_OK if action in ok else sqlite3.SQLITE_DENY


# ---------------------------------------------------------------- prompt injection
INJECTION = [re.compile(p, re.I) for p in (
    r"ignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above)\s+(instructions|prompts?)",
    r"disregard\s+(all\s+|any\s+)?(previous|prior)",
    r"\byou\s+are\s+now\b", r"system\s+prompt", r"^\s*system\s*:",
    r"reveal\s+(all|the)\s+.{0,40}(email|password|customer|data)",
    r"select\s+(\*|\w+(\s*,\s*\w+)*)\s+from\s+\w+", r"</?(system|assistant)>",
)]


def detect_injection(text: str) -> list[str]:
    return [p.pattern for p in INJECTION if p.search(text)]
