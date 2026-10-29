# /// script
# requires-python = ">=3.11"
# dependencies = ["openai>=3.30", "pydantic>=2.12", "python-dotenv>=1.1", "pandas>=2.2", "mcp>=2.3", "streamlit>=1.40"]
# ///
"""Rafeeq's chat app: a citizen talks to the agent; every write action waits for an Approve on a card.

    uv run capstone/rafeeq/app.py          # from labs/; opens http://localhost:8501

The approval is enforced by the harness (rafeeq.Session): a write tool call is queued, not executed, until the
citizen presses Approve here; Reject means it never runs. The chat input is locked while a card is open.
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

import streamlit as st

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

if __name__ == "__main__" and not st.runtime.exists():  # `uv run app.py` → relaunch under Streamlit
    from streamlit.web import cli as stcli

    sys.argv = ["streamlit", "run", __file__, "--server.headless=true", "--browser.gatherUsageStats=false",
                "--theme.base=light", "--theme.primaryColor=#0E6A4E", "--theme.backgroundColor=#F5F7F6",
                "--theme.secondaryBackgroundColor=#E9EFEC", "--theme.textColor=#16302B"]
    sys.exit(stcli.main())

import rafeeq as R  # noqa: E402  (first: it puts labs/ on sys.path)
import agentkit as K  # noqa: E402
import madar  # noqa: E402

ACTIONS = {  # what the citizen reads on the card, per write tool
    "book_inspection": ("Book an inspection", "حجز موعد فحص"),
    "reschedule_inspection": ("Move an inspection", "تغيير موعد الفحص"),
    "create_refund": ("Request a refund", "طلب استرداد"),
    "escalate": ("Escalate to a supervisor", "تصعيد إلى المشرف"),
    "send_email": ("Send an email", "إرسال بريد إلكتروني"),
}
FIELD_NAMES = {"permit_id": "Permit", "slot": "Slot", "new_slot": "New slot", "inspection_id": "Inspection",
               "reason": "Reason", "note": "Note", "to": "To", "subject": "Subject", "body": "Message",
               "national_id_last4": "ID (last 4)", "mobile": "Mobile"}

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@400;500;600&family=Noto+Kufi+Arabic:wght@600;800&display=swap');
:root { --paper:#F5F7F6; --ink:#16302B; --green:#0E6A4E; --line:#D3DDD8; --amber:#A86B12; --red:#9A2F2F; --muted:#5B6F69; }
html, body, [class*="st-"], .stMarkdown, button, input, textarea { font-family: 'IBM Plex Sans Arabic', system-ui, sans-serif; }
.stApp { background: var(--paper); color: var(--ink); }
/* every paragraph takes its own direction: Arabic lines run right-to-left, English left-to-right */
.stMarkdown p, .stMarkdown li, .stChatMessage td, .stChatMessage th { unicode-bidi: plaintext; text-align: start; line-height: 1.7; }
/* a reply written mostly in Arabic flows right-to-left as a whole, so lists and bullets sit on the right */
.stChatMessage:has(.rtl) [data-testid="stChatMessageContent"] { direction: rtl; }
.stChatMessage:has(.rtl) li { unicode-bidi: isolate; text-align: right; }
[data-testid="stChatInput"] textarea { unicode-bidi: plaintext; text-align: start; }
.block-container { max-width: 760px; padding-top: 2.2rem; }

.masthead { display:flex; align-items:baseline; gap:.9rem; border-bottom:1px solid var(--line); padding-bottom:.8rem; margin-bottom:1.2rem; }
.masthead .word { font-family:'Noto Kufi Arabic', sans-serif; font-weight:800; font-size:2.6rem; line-height:1; color:var(--green); }
.masthead .sub { color:var(--muted); font-size:.95rem; } .masthead .sub span { display:block; unicode-bidi:plaintext; }
.masthead .sub b { color:var(--ink); font-weight:600; }

/* the permit slip: the one loud element */
.slip { position:relative; background:#fff; border:1px solid var(--line); border-radius:4px; padding:1.1rem 1.3rem 1rem;
        margin:.6rem 0 .4rem; box-shadow: 0 1px 0 var(--line); }
.slip::before { content:""; position:absolute; inset:0 auto 0 0; width:10px;
        background: radial-gradient(circle at 0 50%, var(--paper) 4px, transparent 4.5px) 0 0/10px 14px repeat-y;
        border-right:1px dashed var(--line); }
.slip .head { display:flex; justify-content:space-between; align-items:flex-start; gap:1rem; padding-left:.6rem; }
.slip h4 { margin:0; font-size:1.15rem; font-weight:600; color:var(--ink); }
.slip h4 span { display:block; font-size:.95rem; font-weight:500; color:var(--muted); direction:rtl; text-align:left; }
.seal { flex:none; width:74px; height:74px; border-radius:50%; border:2px solid currentColor; display:grid; place-items:center;
        text-align:center; font-size:.72rem; font-weight:600; line-height:1.2; transform:rotate(-8deg); }
.seal.pending { color:var(--amber); } .seal.ok { color:var(--green); } .seal.no { color:var(--red); }
.slip dl { display:grid; grid-template-columns:max-content 1fr; gap:.25rem 1rem; margin:.9rem 0 .2rem; padding-left:.6rem; }
.slip dt { color:var(--muted); font-size:.88rem; } .slip dd { margin:0; font-weight:500; unicode-bidi:plaintext; }
.slip .tool { padding-left:.6rem; color:var(--muted); font-size:.8rem; margin-top:.5rem; }
.slip .tool code { background:#EEF3F1; color:var(--ink); padding:.05rem .35rem; border-radius:3px; }
.hint { color:var(--muted); font-size:.88rem; margin:.2rem 0 .8rem; }

section[data-testid="stSidebar"] { border-right:1px solid var(--line); }
.meter { margin:.2rem 0 .9rem; } .meter .row { display:flex; justify-content:space-between; font-size:.9rem; }
.meter .bar { height:6px; background:var(--line); border-radius:3px; overflow:hidden; margin-top:.3rem; }
.meter .bar i { display:block; height:100%; background:var(--green); }
[data-testid="stSidebarContent"] { overflow-x:hidden; }
.audit { overflow-wrap:anywhere; font-size:.8rem; border-left:2px solid var(--line); padding-left:.6rem; }
.audit div { padding:.25rem 0; border-bottom:1px solid #E4EBE8; }
.audit .ev { font-weight:600; } .audit .ev.approved { color:var(--green); } .audit .ev.rejected, .audit .ev.blocked { color:var(--red); }
.audit .ev.approval_requested { color:var(--amber); } .audit .args { color:var(--muted); word-break:break-word; }
@media (prefers-reduced-motion: reduce) { * { transition:none !important; animation:none !important; } }
</style>
"""

st.set_page_config(page_title="Rafeeq · رفيق", page_icon="🟢", layout="centered")
st.markdown(CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------- one agent session per browser tab
def start(user: str, config: str) -> None:
    old = st.session_state.get("mcp")
    if old is not None:
        old.close()
    workdir = R.OUT / "db" / f"app-{user}"
    m = madar.Madar(workdir)  # a fresh database copy; the MCP server reads the same file
    mcp = K.MCPBridge(R.mcp_server(workdir)).open()
    st.session_state.update(mcp=mcp, session=R.Session(user, mcp, m, config=config), msgs=[], decided={},
                            key=(user, config))


with st.sidebar:
    st.markdown("**Who is chatting**")
    user = st.text_input("Citizen name (memory is kept per name)", value=st.session_state.get("key", ("noura",))[0])
    config = st.radio("Configuration", ["A", "B"], horizontal=True, help="A = " + R.CONFIGS["A"]["model"] +
                      "\n\nB = " + R.CONFIGS["B"]["model"])
    if st.session_state.get("key") != (user.strip() or "citizen", config) or st.button("New conversation"):
        with st.spinner("Starting Rafeeq and the Madar MCP server…"):
            start(user.strip() or "citizen", config)

s: R.Session = st.session_state.session


# ---------------------------------------------------------------- rendering helpers
def slip_html(item: dict, state: str = "pending") -> str:
    en, ar = ACTIONS.get(item["tool"], (item["tool"], ""))
    seal = {"pending": "Waiting<br>for you", "ok": "Approved<br>تمت", "no": "Rejected<br>مرفوض"}[state]
    rows = "".join(f"<dt>{html.escape(FIELD_NAMES.get(k, k))}</dt><dd>{html.escape(str(v))}</dd>"
                   for k, v in R._redact(item["args"]).items())
    return (f'<div class="slip"><div class="head"><h4>{html.escape(en)}<span>{html.escape(ar)}</span></h4>'
            f'<div class="seal {state}">{seal}</div></div><dl>{rows}</dl>'
            f'<div class="tool">Tool <code>{html.escape(item["tool"])}</code> runs only if you approve.</div></div>')


def is_arabic(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(bool(R.ARABIC_RE.match(c)) for c in letters) / len(letters) > 0.5


def say(role: str, text: str, slips: list | None = None) -> None:
    st.session_state.msgs.append({"role": role, "text": text, "slips": slips or []})


def agent_reply(reply: R.Reply) -> None:
    say("assistant", reply.text)


# ---------------------------------------------------------------- page
st.markdown('<div class="masthead"><span class="word">رفيق</span><span class="sub"><span><b>Rafeeq</b>, '
            'the Madar permits assistant. Ask in Arabic or English.</span><span>اسأل عن التصاريح والرسوم والمواعيد.</span>'
            '</span></div>',
            unsafe_allow_html=True)

if not st.session_state.msgs:
    remembered = s.memory.load()
    if remembered:
        st.markdown(f'<p class="hint">Welcome back. I remember your language and these permits: '
                    f'{", ".join(remembered.get("permits", [])) or "none yet"}. I will still verify your identity '
                    f'before sharing details.</p>', unsafe_allow_html=True)
    else:
        st.markdown('<p class="hint">For anything about your own application, have your permit ID, the last 4 digits '
                    'of your national ID and your registered mobile ready.</p>', unsafe_allow_html=True)

for msg in st.session_state.msgs:
    with st.chat_message(msg["role"], avatar="🧑" if msg["role"] == "user" else "🟢"):
        if msg["text"]:
            if is_arabic(msg["text"]):
                st.markdown('<span class="rtl"></span>', unsafe_allow_html=True)
            st.markdown(msg["text"])
        for item, state in msg["slips"]:
            st.markdown(slip_html(item, state), unsafe_allow_html=True)

# open approval cards: the only way forward while a write is pending
if s.pending:
    with st.chat_message("assistant", avatar="🟢"):
        for i, item in enumerate(s.pending):
            choice = st.session_state.decided.get(i)
            st.markdown(slip_html(item, {True: "ok", False: "no", None: "pending"}[choice]), unsafe_allow_html=True)
            if choice is None:
                a, r, _ = st.columns([1, 1, 2])
                if a.button("Approve · موافقة", key=f"ok{i}", type="primary"):
                    st.session_state.decided[i] = True
                    st.rerun()
                if r.button("Reject · رفض", key=f"no{i}"):
                    st.session_state.decided[i] = False
                    st.rerun()
        if len(st.session_state.decided) == len(s.pending):
            decisions = [st.session_state.decided[i] for i in range(len(s.pending))]
            done = [(item, "ok" if d else "no") for item, d in zip(s.pending, decisions)]
            with st.spinner("Carrying out your decision…"):
                reply = s.decide(decisions)
            say("assistant", "", done)
            st.session_state.decided = {}
            agent_reply(reply)
            st.rerun()
        else:
            st.markdown('<p class="hint">Nothing changes until you choose. لن يتغير شيء قبل اختيارك.</p>',
                        unsafe_allow_html=True)

prompt = st.chat_input("Write your message · اكتب رسالتك", disabled=bool(s.pending))
if prompt:
    say("user", prompt)
    with st.spinner("Rafeeq is checking…"):
        reply = s.send(prompt)
    agent_reply(reply)
    st.rerun()


# ---------------------------------------------------------------- sidebar: budgets, memory, audit
def short(args: dict, limit: int = 140) -> str:
    text = json.dumps(args, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + " …"


def meter(label: str, used: float, limit: float, fmt: str) -> str:
    pct = min(100, round(100 * used / limit)) if limit else 0
    return (f'<div class="meter"><div class="row"><span>{label}</span><span>{fmt.format(used)} / {fmt.format(limit)}</span>'
            f'</div><div class="bar"><i style="width:{pct}%"></i></div></div>')


with st.sidebar:
    st.divider()
    st.markdown("**This conversation**")
    st.markdown(meter("Model steps", s.steps, R.MAX_STEPS * max(1, sum(m["role"] == "user" for m in st.session_state.msgs)), "{:,.0f}")
                + meter("Tokens", s.tokens, R.MAX_TOKENS * max(1, sum(m["role"] == "user" for m in st.session_state.msgs)), "{:,.0f}")
                + f'<div class="meter"><div class="row"><span>Cost</span><span>${s.cost:.4f} of ${R.MAX_COST} a message</span></div></div>',
                unsafe_allow_html=True)
    st.caption(f"Per message: at most {R.MAX_STEPS} model steps and {R.MAX_TOKENS:,} tokens.")
    mem = s.memory.load()
    st.markdown("**Remembered across sessions**")
    st.markdown(f"Language: {mem.get('language', '—')}  \nPermits: {', '.join(mem.get('permits', [])) or '—'}")
    st.markdown("**Audit log**")
    rows = "".join(f'<div><span class="ev {e["event"]}">{html.escape(e["event"].replace("_", " "))}</span> '
                   f'{html.escape(e["tool"])}<div class="args">{html.escape(short(e["args"]))}</div></div>'
                   for e in reversed(s.audit.events[-14:]))
    st.markdown(f'<div class="audit">{rows or "<div>No tool calls yet.</div>"}</div>', unsafe_allow_html=True)
