"""python -m copilot.cli seed | triage [--llm openai|gemini] | pending | approve ID | reject ID | audit"""
from __future__ import annotations

import argparse
import os
from collections import Counter

from . import db
from .tools import build_registry
from .triage import LLMClassifier, TriageAgent

DB = os.getenv("COPILOT_DB", "data/crm.db")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    t = sub.add_parser("triage"); t.add_argument("--llm", choices=["openai", "gemini"])
    sub.add_parser("pending"); sub.add_parser("audit")
    for name in ("approve", "reject"):
        p = sub.add_parser(name); p.add_argument("id", type=int); p.add_argument("--by", default="human")
    a = ap.parse_args()

    conn = db.connect(DB)
    reg = build_registry(conn)
    if a.cmd == "seed":
        db.seed(conn); print(f"Seeded {DB}")
    elif a.cmd == "triage":
        agent = TriageAgent(reg, LLMClassifier(a.llm) if a.llm else None)
        results = agent.process_all()
        for r in results:
            print(f"#{r.ticket_id:<3} {r.outcome:<17} {r.category:<16} {r.priority:<7} conf={r.confidence:<4} {'; '.join(r.reasons)}")
        c = Counter(r.outcome for r in results)
        n = len(results)
        print(f"\n{n} tickets: " + ", ".join(f"{k}={v}" for k, v in c.items()) +
              f"  | fully automated: {c['auto_triaged']}/{n}")
    elif a.cmd == "pending":
        for r in reg.pending_approvals():
            print(r["id"], r["tool"], r["args_json"])
    elif a.cmd in ("approve", "reject"):
        print(reg.decide(a.id, a.by, a.cmd == "approve"))
    elif a.cmd == "audit":
        for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 30"):
            print(r["ts"], r["actor"], r["tool"], r["outcome"])


if __name__ == "__main__":
    main()
