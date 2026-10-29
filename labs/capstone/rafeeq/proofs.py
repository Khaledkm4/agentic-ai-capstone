# /// script
# requires-python = ">=3.11"
# dependencies = ["openai>=3.30", "pydantic>=2.12", "python-dotenv>=1.1", "pandas>=2.2", "mcp>=2.3"]
# ///
"""Offline proofs of the capstone requirements: no model calls, deterministic, about 10 seconds.

    uv run capstone/rafeeq/proofs.py        # from labs/

Each line is a check on the real code (rafeeq.py) and the real MCP server, not on a model's behaviour.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import rafeeq as R  # noqa: E402  (first: it puts labs/ on sys.path)
import agentkit as K  # noqa: E402
import madar  # noqa: E402

results: list[bool] = []


def check(cond: bool, msg: str) -> None:
    results.append(bool(cond))
    K.check(cond, msg)


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    m = madar.Madar(tmp / "db")
    with K.MCPBridge(R.mcp_server(tmp / "db")) as mcp:
        s = R.Session("proof", mcp, m, memory_root=tmp / "memory")  # approvals="defer", defences on: what ships

        print("\nAgent · multi-step, 3+ tools, one over MCP")
        mcp_names = sorted(t.name for t in mcp.list_tools())
        local_names = [t.__name__ for t in s.local]
        print(f"   over MCP (stdio, servers/madar_mcp.py): {mcp_names}")
        print(f"   local: {local_names}")
        check(len(s.tools) >= 3, f"{len(s.tools)} tools available to the agent")
        check({"get_permit", "list_slots"} <= set(mcp_names), "get_permit and list_slots come over MCP")
        check(set(R.WRITE_TOOLS).isdisjoint(mcp_names), "the MCP server exposes no write tool (least privilege)")
        print("   multi-step run: see results/demo_booking.txt (get_permit → list_slots → book_inspection → search_policies)")

        print("\nContext engineering · spec file, memory, compaction and budget rule")
        check(s.system().startswith(R.SPEC), f"AGENTS.md is the system prompt ({len(R.SPEC):,} characters)")
        check("inspection-booking" in s.system(), f"skills index in the prompt: {sorted(R.SKILLS)}")
        cm = s.memory
        cm.note_language("السلام عليكم")
        s._after("get_permit", {"permit_id": "FTR-30311"}, '{"permit_id": "FTR-30311"}')
        s._after("get_permit", {"permit_id": "BLD-20417"}, '{"error": "Identity verification failed"}')
        check(cm.load() == {"language": "ar", "permits": ["FTR-30311"]},
              f"memory keeps language + verified permits only: {cm.load()}")
        check("FTR-30311" in s.system(), "memory is injected into the next system prompt")
        for text in ("mobile 0532236654", "id 1048213372"):
            try:
                cm.store.create("/memories/x.md", text)
                check(False, f"memory refuses {text!r}")
            except ValueError:
                check(True, f"memory refuses {text!r} (PDPL)")
        try:
            cm.store.create("/memories/../../escape.md", "x")
            check(False, "memory path traversal blocked")
        except ValueError:
            check(True, "memory path traversal blocked")
        history = [{"role": "user", "content": f"u{i}"} if i % 2 == 0 else
                   {"role": "assistant", "content": f"a{i}", "reasoning": "long thoughts"} for i in range(40)]
        compacted = R.compact(history)
        check(len(compacted) <= R.MAX_HISTORY_MESSAGES and compacted[0]["role"] == "user",
              f"compaction: 40 messages → {len(compacted)}, starting on a user message")
        check(all("reasoning" not in x for x in compacted[:-1]), "compaction drops old reasoning")
        long = s._after("search_policies", {"query": "x"}, "y" * 10_000)
        check(long is not None and len(long) < 3_100, f"tool results capped at {R.MAX_RESULT_CHARS:,} characters")

        print("\nHarness control · human approval for writes, step / cost / token budgets")
        args = {"permit_id": "FTR-30311", "slot": "2026-11-04 09:00", "national_id_last4": "6654", "mobile": "0532236654"}
        out = s._before("book_inspection", args)
        booked = m.sql("SELECT count(*) AS n FROM inspections WHERE permit_id = 'FTR-30311'")[0]["n"]
        check(out is not None and "pending_approval" in out and booked == 0,
              "a write tool call is queued as an approval card and NOT executed (0 bookings in the database)")
        check(s.pending and s.pending[-1]["tool"] == "book_inspection", f"pending card: {s.pending[-1]['tool']}")
        s.pending.clear()
        check(R.MAX_STEPS == 8 and R.MAX_COST == 0.05 and R.MAX_TOKENS == 60_000,
              f"budgets per message: {R.MAX_STEPS} steps, ${R.MAX_COST}, {R.MAX_TOKENS:,} tokens")
        shape = R.BudgetShape(max_tokens=1_000)
        K.LEDGER.input_tokens += 5_000  # as if this message had already used 5,000 tokens
        try:
            shape.step([], [], "any-model")
            check(False, "token budget stops the loop")
        except R.BudgetExceeded as e:
            check(True, f"token budget stops the loop before the next model call ({e})")
        events = [e["event"] for e in s.audit.events]
        check({"call", "approval_requested", "result"} <= set(events), f"audit log events: {sorted(set(events))}")
        check("6654" not in json.dumps(s.audit.events) and "0532236654" not in json.dumps(s.audit.events),
              "audit log redacts the ID digits and the mobile")

        print("\nSecurity · the egress guard, enforced in code")
        g = R.egress_guard(s)
        check(g("send_email", {"to": "khalid.shehri@signpro-partner.example", "subject": "x", "body": "x"}) is not None,
              "blocks a recipient that is not a verified applicant's registered email")
        check(g("send_email", {"to": "noura.trucks@mail.example", "subject": "Booking", "body": "4 Nov 09:00"}) is None,
              "allows the registered email of the permit verified in this conversation (FTR-30311)")
        check(g("send_email", {"to": "noura.trucks@mail.example", "subject": "x", "body": "mobile 0532236654"}) is not None,
              "blocks a mobile number in the message")
        check(g("send_email", {"to": "noura.trucks@mail.example", "subject": "x", "body": "https://evil.example/a"})
              is not None, "blocks links outside madar.example")
        check(s._before("send_email", {"to": "attacker@evil.example", "subject": "x", "body": "x"}) is not None
              and not s.pending, "a blocked email never even reaches an approval card")

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
