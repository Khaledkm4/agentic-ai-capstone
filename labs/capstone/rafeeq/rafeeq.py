# /// script
# requires-python = ">=3.11"
# dependencies = ["openai>=3.30", "pydantic>=2.12", "python-dotenv>=1.1", "pandas>=2.2", "mcp>=2.3"]
# ///
"""Rafeeq in production (capstone, Track 1): the Madar assistant as a complete service.

    uv run capstone/rafeeq/rafeeq.py ask "What is the fee for an illuminated 3 m² sign?"
    uv run capstone/rafeeq/rafeeq.py chat --user noura          # a conversation; memory kept per user
    uv run capstone/rafeeq/rafeeq.py demo                       # the scripted booking workflow, end to end
    uv run capstone/rafeeq/rafeeq.py eval capstone/rafeeq/tasks.jsonl --config A B

Run from labs/ (it imports agentkit, madar and evals from there). Only OPENROUTER_API_KEY is needed.

What it adds to the course's capstone starter:
1. tools: Madar's read tools over MCP (servers/madar_mcp.py), the write tools locally, and load_skill;
2. context: AGENTS.md as the spec, skills by progressive disclosure, a capped cross-session memory (language and
   verified permit IDs only), history compaction that also drops old reasoning, a cap on tool-result size;
3. harness: write tools never run without a human decision (an approval card in the app, y/N in the CLI), step,
   cost and token budgets, an audit log with redacted arguments;
4. evals: the Day 3 runner on two configurations, one CSV per configuration in capstone/rafeeq/results/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
LABS = HERE.parents[1]
sys.path.insert(0, str(LABS))

from dotenv import load_dotenv  # noqa: E402

# The project's models, so a fresh clone needs only OPENROUTER_API_KEY. agentkit reads these at import; a value set
# in labs/.env still wins (it is loaded first), and the course defaults (rate-limited Gemma models) are never used.
load_dotenv(LABS / ".env")
os.environ.setdefault("AGENT_MODEL", "nvidia/nemotron-3.5-super-vl:free")
os.environ.setdefault("AGENT_SMALL_MODEL", "nvidia/nemotron-3.5-lightning:free")  # the eval judge
os.environ.setdefault("AGENT_FALLBACKS", "nvidia/nemotron-3-ultra-550b-a55b:free,nvidia/nemotron-3.5-lightning:free")

from mcp import StdioServerParameters  # noqa: E402

import agentkit as K  # noqa: E402
import evals  # noqa: E402
import madar  # noqa: E402

OUT = K.lab_dir("rafeeq")  # runtime data (databases, memory, audit log): labs/artifacts/rafeeq, git-ignored
RESULTS = HERE / "results"

# ---------------------------------------------------------------- spec and configurations
SPEC = (HERE / "AGENTS.md").read_text(encoding="utf-8")
CLOCK = "Today is Sunday 1 November 2026, 09:00 Riyadh time (from the system clock, not from memory)."

# Requirement 4: two configurations that differ in ONE thing. Here: the model. Same spec, tools, harness, budgets.
MODEL_B = "nvidia/nemotron-3-ultra-550b-a55b:free"
CONFIGS = {
    "A": {"model": K.MODEL},  # AGENT_MODEL: nvidia/nemotron-3.5-super-vl:free unless labs/.env says otherwise
    "B": {"model": MODEL_B},  # the 550B Nemotron: is a failure a capability problem or a harness problem?
}

MAX_STEPS = 8  # model calls per citizen message
MAX_COST = 0.05  # USD per citizen message (free models report $0, so the token budget is the one that bites)
MAX_TOKENS = 60_000  # input + output tokens per citizen message
MAX_RESULT_CHARS = 3_000
MAX_HISTORY_MESSAGES = 24

WRITE_TOOLS = {"book_inspection", "reschedule_inspection", "create_refund", "escalate", "send_email"}

# ---------------------------------------------------------------- skills (Lab 2.3, progressive disclosure)
SKILLS_DIR = K.DATA / "skills"


def _read_skill(path: Path) -> tuple[dict, str]:
    _, head, body = path.read_text(encoding="utf-8").split("---", 2)
    return dict(line.split(": ", 1) for line in head.strip().splitlines() if ": " in line), body.strip()


SKILLS = {_read_skill(p)[0]["name"]: p for p in sorted(SKILLS_DIR.glob("*/SKILL.md"))}
SKILL_INDEX = "\n".join(f"- **{n}**: {_read_skill(p)[0]['description']}" for n, p in SKILLS.items())


def load_skill(name: str) -> str:
    """Load the step-by-step procedure for a multi-step task before you start it.

    Args:
        name: one of inspection-booking, refund-request, escalation, appeal.
    """
    if name not in SKILLS:
        return json.dumps({"error": f"No skill {name!r}. Available: {sorted(SKILLS)}"})
    return _read_skill(SKILLS[name])[1]


# ---------------------------------------------------------------- memory (Lab 2.3's capped store + a PII guard)
NATIONAL_ID_RE = re.compile(r"\b[12]\d{9}\b")
MOBILE_RE = re.compile(r"(?:\+966|00966|0)?5\d{8}\b")
PERMIT_RE = re.compile(r"\b(?:BLD|REN|SGN|FTR|EVT)-\d{5}\b", re.I)
ARABIC_RE = re.compile(r"[؀-ۿ]")


class Memory:
    """The memory-tool store of Lab 2.3: files under /memories, path-confined and size-capped.

    Added for production: `create` refuses anything that looks like a national ID or a mobile number (PDPL), so
    even a buggy or injected writer can't put personal identifiers on disk.
    """

    MAX_FILE = 600
    MAX_TOTAL = 1_200

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, path: str) -> Path:
        if not path or not (path == "/memories" or path.startswith("/memories/")):
            raise ValueError(f"Paths must start with /memories, got {path!r}")
        real = (self.root / path.removeprefix("/memories").lstrip("/")).resolve()
        if real != self.root and self.root not in real.parents:
            raise ValueError(f"Path escapes the memory directory: {path!r}")
        return real

    def _total(self, excluding: Path | None = None) -> int:
        return sum(len(p.read_text(encoding="utf-8")) for p in self.root.rglob("*") if p.is_file() and p != excluding)

    def read(self, path: str) -> str:
        p = self._resolve(path)
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    def create(self, path: str, file_text: str) -> str:
        p = self._resolve(path)
        if NATIONAL_ID_RE.search(file_text) or MOBILE_RE.search(file_text):
            raise ValueError("Refused: memory may not hold national IDs or mobile numbers (PDPL).")
        if len(file_text) > self.MAX_FILE:
            raise ValueError(f"File too long ({len(file_text)} > {self.MAX_FILE} chars). Keep only what matters.")
        if self._total(excluding=p) + len(file_text) > self.MAX_TOTAL:
            raise ValueError("Memory is full. Delete or shorten old memories first.")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(file_text, encoding="utf-8")
        return f"Created {path}"


class CitizenMemory:
    """What Rafeeq carries from one session to the next, for one citizen: preferred language and permit IDs.

    The harness writes it, not the model: the language from the citizen's own messages, a permit ID only after a
    tool verified the citizen's identity for it. So an injected instruction can't make Rafeeq remember something,
    and the memory never links a person to a permit they haven't proved is theirs.
    """

    PATH = "/memories/profile.json"
    MAX_PERMITS = 5  # curation: keep the most recent five

    def __init__(self, root: Path):
        self.store = Memory(root)

    def load(self) -> dict:
        try:
            return json.loads(self.store.read(self.PATH) or "{}")
        except json.JSONDecodeError:
            return {}

    def _save(self, data: dict) -> None:
        self.store.create(self.PATH, json.dumps(data, ensure_ascii=False))

    def note_language(self, message: str) -> None:
        letters = re.findall(r"[^\W\d_]", message)
        if not letters:
            return
        lang = "ar" if sum(bool(ARABIC_RE.match(c)) for c in letters) / len(letters) > 0.3 else "en"
        data = self.load()
        if data.get("language") != lang:
            self._save({**data, "language": lang})

    def note_permit(self, permit_id: str) -> None:
        pid = permit_id.strip().upper()
        if not PERMIT_RE.fullmatch(pid):
            return
        data = self.load()
        permits = [p for p in data.get("permits", []) if p != pid] + [pid]
        self._save({**data, "permits": permits[-self.MAX_PERMITS:]})

    def block(self) -> str:
        data = self.load()
        if not data:
            return "## Memory\n(nothing remembered about this citizen yet)"
        lang = {"ar": "Arabic", "en": "English"}.get(data.get("language", ""), "unknown")
        permits = ", ".join(data.get("permits", [])) or "none"
        return (f"## Memory (from earlier sessions)\n- Preferred language: {lang}\n- Permits this citizen verified "
                f"before: {permits} (still verify again before sharing any detail)")


# ---------------------------------------------------------------- audit log
def _redact(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        if k == "national_id_last4":
            out[k] = "****"
        elif k == "mobile":
            out[k] = "*******" + str(v)[-2:]
        elif isinstance(v, str):
            out[k] = MOBILE_RE.sub("[mobile]", NATIONAL_ID_RE.sub("[national-id]", v))
        else:
            out[k] = v
    return out


class Audit:
    """Append-only JSONL: every tool call, approval request and decision, with personal data redacted."""

    def __init__(self, path: Path, session: str):
        self.path, self.session = path, session
        self.events: list[dict] = []

    def log(self, event: str, tool: str, args: dict | None = None, **extra) -> None:
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": self.session, "event": event, "tool": tool,
               "args": _redact(args or {}), **extra}
        self.events.append(row)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- budgets
class BudgetExceeded(Exception):
    pass


class BudgetShape(K.ChatShape):
    """Chat Completions with a token budget per citizen message (run_agent's max_cost is $0 on free models)."""

    def __init__(self, max_tokens: int):
        super().__init__()
        self.max_tokens = max_tokens
        self.base = K.LEDGER.input_tokens + K.LEDGER.output_tokens

    def used(self) -> int:
        return K.LEDGER.input_tokens + K.LEDGER.output_tokens - self.base

    RETRIES = 2

    def step(self, state, tools, model, use_cache=None, **kw):
        for attempt in range(self.RETRIES + 1):
            if self.used() > self.max_tokens:
                raise BudgetExceeded(f"token budget of {self.max_tokens:,} exceeded ({self.used():,} used)")
            turn = super().step(state, tools, model, use_cache=use_cache if attempt == 0 else False, **kw)
            # free providers sometimes end a turn with finish_reason "error" (or nothing at all): that's an outage,
            # not an answer. Drop it and ask again live (a cached failure would otherwise replay forever).
            reason = turn.raw.choices[0].finish_reason if turn.raw is not None else "stop"
            if turn.calls or (turn.text.strip() and reason != "error") or attempt == self.RETRIES:
                return turn
            state.pop()
        return turn


JUDGE_SYSTEM = evals.JUDGE_PROMPT


def judge(rubric: str, prompt: str, reply: str, tools_used: list[str], model: str | None = None) -> dict:
    """evals.judge with room to think: the free judge (a reasoning model) spends evals' 300 tokens on reasoning
    and returns nothing. Same prompt, same judge model for both configurations, so A and B are graded alike."""
    user = f"RUBRIC:\n{rubric}\n\nUSER MESSAGE:\n{prompt}\n\nTOOLS THE ASSISTANT CALLED: {tools_used or 'none'}\n\nREPLY:\n{reply}"
    for attempt in range(2):
        resp = K.chat([{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}],
                      model=model or K.SMALL_MODEL, temperature=0, max_tokens=4000, fallbacks=False,
                      use_cache=None if attempt == 0 else False)
        text = (K.text_of(resp) or "").strip()
        try:
            data = json.loads(text[text.find("{"): text.rfind("}") + 1])
            return {"pass": bool(data.get("pass")), "reason": str(data.get("reason", ""))}
        except (ValueError, AttributeError):
            continue
    return {"pass": False, "reason": f"unparseable judge output: {text[:120]}"}


evals.judge = judge  # evals.grade looks the judge up at call time


def compact(history: list) -> list:
    """Context rule: keep the last messages (never starting on a tool result) and drop old reasoning text."""
    tail = history[-MAX_HISTORY_MESSAGES:] if len(history) > MAX_HISTORY_MESSAGES else list(history)
    while tail and tail[0].get("role") != "user":
        tail = tail[1:]
    last_user = max((i for i, msg in enumerate(tail) if msg.get("role") == "user"), default=0)
    return [{k: v for k, v in msg.items() if k not in ("reasoning", "reasoning_details")} if i < last_user else msg
            for i, msg in enumerate(tail)]


# ---------------------------------------------------------------- MCP
def mcp_server(workdir: Path | None = None) -> StdioServerParameters:
    """Madar's read tools over MCP (stdio). With `workdir`, the server uses the same database file as the session."""
    args = ["run", "--quiet", str(LABS / "servers" / "madar_mcp.py")]
    return StdioServerParameters(command="uv", args=args + (["--workdir", str(workdir)] if workdir else []))


# ---------------------------------------------------------------- security (Phase 5): the egress guard
LINK_RE = re.compile(r"https?://([^/\s)>\]\"']+)", re.I)
TRUSTED_LINK_DOMAIN = "madar.example"


def egress_guard(session: "Session") -> Callable[[str, dict], str | None]:
    """Cut the egress leg of the trifecta in code (POL-10), whatever the model was talked into.

    send_email may only go to the registered email of an applicant whose identity this session verified with
    get_permit; its subject and body may not carry a national ID or a mobile number; links only to madar.example.
    It runs before the approval card is shown (the citizen never sees a card for a blocked email) and again
    when an approved action is executed.
    """

    def guard(name: str, args: dict) -> str | None:
        if name != "send_email":
            return None
        to = str(args.get("to", "")).strip().lower()
        allowed = set()
        if session.verified:
            marks = ",".join("?" * len(session.verified))
            allowed = {r["email"].lower() for r in session.m.sql(
                f"SELECT a.email FROM permits p JOIN applicants a USING (applicant_id) WHERE p.permit_id IN ({marks})",
                tuple(session.verified))}
        text = f"{args.get('subject', '')}\n{args.get('body', '')}"
        reasons = []
        if to not in allowed:
            reasons.append("the recipient is not the registered email of an applicant verified in this conversation "
                           "(POL-10: email only the applicant)")
        if NATIONAL_ID_RE.search(text) or MOBILE_RE.search(text):
            reasons.append("the message contains a national ID or a mobile number")
        if any(not h.lower().endswith(TRUSTED_LINK_DOMAIN) for h in LINK_RE.findall(text)):
            reasons.append(f"links may only point to {TRUSTED_LINK_DOMAIN}")
        if not reasons:
            return None
        return json.dumps({"error": "Blocked by Madar's egress policy: " + "; ".join(reasons) + ". Do not retry with "
                           "another address. Tell the citizen it can only be sent to their registered email."})

    return guard


# ---------------------------------------------------------------- one conversation
@dataclass
class Reply:
    text: str
    pending: list[dict] = field(default_factory=list)
    run: K.Run | None = None
    stopped: str = "done"
    tokens: int = 0


class Session:
    """One citizen's conversation with Rafeeq: history, memory, budgets, approvals and audit.

    approvals="defer" (app, CLI): a write tool call is queued as a pending action and NOT executed; the citizen
    approves or rejects it with `decide()`, which executes it (or not) in code and lets the model confirm.
    approvals="auto" (evals): the task prompt already gives consent; the db graders check what was written.
    """

    def __init__(self, user: str, mcp: K.MCPBridge, m: madar.Madar, config: str = "A", approvals: str = "defer",
                 memory_root: Path | None = None, verbose: bool = False, model_kw: dict | None = None,
                 defences: bool = True):
        self.user, self.mcp, self.m, self.config = user, mcp, m, CONFIGS[config]
        self.approvals, self.verbose, self.model_kw = approvals, verbose, model_kw or {}
        self.memory = CitizenMemory(memory_root or OUT / "memory" / re.sub(r"[^\w.-]+", "_", user))
        self.audit = Audit(OUT / "audit.jsonl", session=f"{user}:{int(time.time())}")
        self.history: list = []
        self.pending: list[dict] = []
        self.steps = self.tokens = 0
        self.cost = 0.0
        self.verified: set[str] = set()  # permit IDs whose identity check passed in this conversation
        # before_tool checks enforced in code (Phase 5); defences=False only to demonstrate the attack
        self.guards: list[Callable[[str, dict], str | None]] = [egress_guard(self)] if defences else []
        self.spec_extra = ""  # an experimental addition to the spec (error-analysis fixes), off by default
        self.local = [m.book_inspection, m.reschedule_inspection, m.create_refund, m.escalate, m.send_email, load_skill]
        self.tools = self.local + mcp.agent_tools()
        self.by_name = {t.__name__: t for t in self.tools}

    # -- the system prompt is rebuilt every turn so memory is current
    def system(self) -> str:
        return "\n\n".join(x for x in [SPEC, self.spec_extra, CLOCK, "## Skills (load_skill)\n" + SKILL_INDEX,
                                       self.memory.block()] if x)

    # -- harness hooks
    def _permission(self, name: str, args: dict) -> str:
        return "ask" if name in WRITE_TOOLS and self.approvals == "auto" else "allow"

    def _consent(self, name: str, args: dict) -> bool:
        self.audit.log("auto_consent", name, args)
        return True

    def _before(self, name: str, args: dict) -> str | None:
        self.audit.log("call", name, args)
        for guard in self.guards:
            blocked = guard(name, args)
            if blocked is not None:
                self.audit.log("blocked", name, args, reason=blocked[:200])
                return blocked
        if name in WRITE_TOOLS and self.approvals == "defer":
            self.pending.append({"tool": name, "args": args})
            self.audit.log("approval_requested", name, args)
            return json.dumps({"status": "pending_approval", "message": (
                f"{name} was NOT executed. The citizen now sees an approval card for it. Do not call it again. "
                "Tell the citizen, in their language, what will happen and to press Approve or Reject.")})
        return None

    def _after(self, name: str, args: dict, result: str) -> str | None:
        ok = not result.startswith('{"error"') and '"pending_approval"' not in result
        self.audit.log("result", name, args, ok=ok, chars=len(result))
        if ok and "permit_id" in args and (name == "get_permit" or name in WRITE_TOOLS):
            self.memory.note_permit(args["permit_id"])  # identity was verified by the tool for this permit
            self.verified.add(str(args["permit_id"]).strip().upper())
        if len(result) > MAX_RESULT_CHARS:
            return result[:MAX_RESULT_CHARS] + f"\n[… {len(result) - MAX_RESULT_CHARS} characters cut; ask for less]"
        return None

    # -- one model turn
    def _run(self, message: str, use_cache: bool | None = None) -> Reply:
        shape = BudgetShape(MAX_TOKENS)
        try:
            run = K.run_agent(message, self.tools, system=self.system(), model=self.config["model"], shape=shape,
                              history=compact(self.history), max_steps=MAX_STEPS, max_cost=MAX_COST,
                              permission=self._permission, approve=self._consent, before_tool=self._before,
                              after_tool=self._after, verbose=self.verbose, use_cache=use_cache, **self.model_kw)
        except BudgetExceeded as e:
            self.audit.log("budget", "-", {}, reason=str(e))
            return Reply("I had to stop: this request used up its budget. An officer will follow up. "
                         "توقفت لأن الطلب تجاوز الميزانية المحددة، وسيتابع معك موظف.", stopped="max_tokens",
                         tokens=shape.used())
        self.history = run.state[1:]  # without the system prompt, which is rebuilt next turn
        self.steps += run.steps
        self.cost += run.cost
        self.tokens += shape.used()
        if run.stopped != "done":
            self.audit.log("budget", "-", {}, reason=run.stopped)
        text = run.final or f"(stopped: {run.stopped}) I couldn't finish this; an officer will follow up."
        return Reply(text, list(self.pending), run, run.stopped, shape.used())

    def send(self, message: str, use_cache: bool | None = None) -> Reply:
        self.memory.note_language(message)
        return self._run(message, use_cache)

    def decide(self, approved: bool | list[bool]) -> Reply:
        """The citizen's answer to the pending approval card(s). Approved actions are executed here, in code."""
        decisions = approved if isinstance(approved, list) else [approved] * len(self.pending)
        lines = []
        for item, ok in zip(self.pending, decisions):
            name, args = item["tool"], item["args"]
            if ok:
                self.audit.log("approved", name, args)
                blocked = next((b for g in self.guards if (b := g(name, args)) is not None), None)
                result = blocked or K.call_tool(self.by_name, name, args)
                result = self._after(name, args, result) or result
                lines.append(f"- {name}({json.dumps(args, ensure_ascii=False)}): APPROVED by the citizen and "
                             f"executed. Result: {result}")
            else:
                self.audit.log("rejected", name, args)
                lines.append(f"- {name}({json.dumps(args, ensure_ascii=False)}): REJECTED by the citizen. Not executed.")
        self.pending = []
        note = ("[Harness note, not written by the citizen] The citizen answered the approval card(s):\n"
                + "\n".join(lines) + "\nTell the citizen the outcome in their language (for a booking: the slot, the "
                "district and what to prepare). Do not call the same write tool again.")
        return self._run(note)


# ---------------------------------------------------------------- the eval agent
def make_eval_agent(config: str, mcp: K.MCPBridge, guards: list | None = None, spec_extra: str = "",
                    defences: bool = True):
    """An `evals.run_suite` agent: a fresh session per task (no shared history, throwaway memory).
    `agent.tokens` records the tokens of each task's run (by prompt), for the CSV."""
    tokens: dict[str, int] = {}

    def agent(prompt: str, m: madar.Madar, use_cache: bool | None = None, **kw) -> K.Run:
        s = Session(f"eval-{config}", mcp, m, config=config, approvals="auto", memory_root=m.workdir / "memory",
                    model_kw={"fallbacks": False}, defences=defences)  # no silent model swap: A stays A, B stays B
        s.guards, s.spec_extra = s.guards + list(guards or []), spec_extra
        reply = s.send(prompt, use_cache=use_cache)
        tokens[prompt] = reply.tokens
        if reply.run is None:  # budget stop
            return K.Run(reply.text, [], [], 0, reply.stopped)
        return reply.run

    agent.__name__, agent.tokens = config, tokens
    return agent


# ---------------------------------------------------------------- CLI
def approve_cli(pending: list[dict]) -> list[bool]:
    out = []
    for p in pending:
        print(f"\n🛡️  APPROVAL CARD · {p['tool']}({json.dumps(p['args'], ensure_ascii=False)})")
        out.append(input("    Approve? [y/N] ").strip().lower() == "y")
    return out


def show(reply: Reply, s: Session) -> None:
    print(f"\n🤖 {reply.text}")
    print(f"   [steps so far {s.steps} · tokens {s.tokens:,} · cost ${s.cost:.4f}]")


DEMO = [  # the booking workflow: verify → check permit → slots → approval → book → confirm
    "Hi, I'm Noura. My food truck permit FTR-30311 was approved and I need the health inspection. "
    "National ID ends 6654, mobile 0532236654. What slots do you have on Wednesday 4 or Thursday 5 November?",
    "The Wednesday 4 November slot at 09:00 please. Yes, book it.",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--config", default="A")
    c = sub.add_parser("chat")
    c.add_argument("--user", default="citizen")
    c.add_argument("--config", default="A")
    d = sub.add_parser("demo")
    d.add_argument("--yes", action="store_true", help="approve the card automatically (recording a demo)")
    d.add_argument("--config", default="A")
    e = sub.add_parser("eval")
    e.add_argument("tasks", type=Path)
    e.add_argument("--config", nargs="+", default=["A", "B"])
    e.add_argument("--ids", nargs="*", help="only these task IDs")
    e.add_argument("--limit", type=int, help="only the first N tasks")
    e.add_argument("--trials", type=int, default=1)
    e.add_argument("--tag", default="", help="suffix for the CSV names, e.g. 'smoke'")
    e.add_argument("--spec-extra", default="", help="text appended to the spec (an error-analysis experiment)")
    args = ap.parse_args()

    if args.cmd == "eval":
        tasks = evals.load_tasks(args.tasks, ids=args.ids)[: args.limit]
        RESULTS.mkdir(exist_ok=True)
        with K.MCPBridge(mcp_server(OUT / "mcp_eval")) as mcp:
            for cfg in args.config:
                print(f"\n=== config {cfg}: {CONFIGS[cfg]['model']} on {len(tasks)} tasks")
                agent = make_eval_agent(cfg, mcp, spec_extra=args.spec_extra)
                df = evals.run_suite(agent, tasks, trials=args.trials, name=cfg)
                df["model"] = CONFIGS[cfg]["model"]
                df["tokens"] = df.id.map({t["id"]: agent.tokens.get(t["prompt"], 0) for t in tasks})
                path = RESULTS / f"eval_{cfg}{'_' + args.tag if args.tag else ''}.csv"
                df.to_csv(path, index=False, encoding="utf-8")
                overall, _ = evals.report(df)
                print(overall, "\n→", path)
        print("\n", K.LEDGER)
        return 0

    workdir = OUT / "db" / (getattr(args, "user", None) or args.cmd)
    m = madar.Madar(workdir)  # a fresh copy of the database for this run
    with K.MCPBridge(mcp_server(workdir)) as mcp:  # the MCP server reads the same database file
        if args.cmd == "ask":
            s = Session("ask", mcp, m, config=args.config, verbose=True)
            reply = s.send(args.question)
            show(reply, s)
            if reply.pending:
                show(s.decide(approve_cli(reply.pending)), s)
        elif args.cmd == "demo":
            s = Session("noura-demo", mcp, m, config=args.config, verbose=True)
            for msg in DEMO:
                print(f"\n🧑 {msg}")
                reply = s.send(msg)
                show(reply, s)
                if reply.pending:
                    decisions = [True] * len(reply.pending) if args.yes else approve_cli(reply.pending)
                    if args.yes:
                        print("\n🛡️  APPROVAL CARD →", reply.pending, "→ approved (--yes)")
                    show(s.decide(decisions), s)
            print("\n📒 database:", m.sql("SELECT inspection_id, permit_id, kind, slot, district, status FROM inspections "
                                           "WHERE permit_id = 'FTR-30311'"))
            print("🧠 memory:", s.memory.load())
            print("🧾 audit (this session):")
            for ev in s.audit.events:
                print("  ", json.dumps({k: ev[k] for k in ("event", "tool", "args")}, ensure_ascii=False))
        else:
            s = Session(args.user, mcp, m, config=args.config, verbose=True)
            print(f"Memory for {args.user}: {s.memory.load() or '(empty)'}\nType your message (empty line to quit).")
            while (msg := input("\n🧑 ").strip()):
                reply = s.send(msg)
                show(reply, s)
                while reply.pending:
                    reply = s.decide(approve_cli(reply.pending))
                    show(reply, s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
