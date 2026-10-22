"""Scenario runner: S1-S7 against a fresh temp DB each, deterministic assertions, no LLM judge.

    python -m scenarios.run [--only 1,4] [--keep-db] [--dry]

Each scenario runs in its own child process with DB_PATH / CALL_LOG_PATH pointing at a temp dir,
set before chatbot is imported, so the repo's data/ and logs/ are never touched.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scenarios import checks  # noqa: E402
from scenarios.linkedin_rules import check_post, extract_post  # noqa: E402
from scripts import gen_history  # noqa: E402

USER = "scenario-user"
DM = "slack-dm"
RESULT_TAG = "SCENARIO_RESULT "
NAMES = {1: "I just told you", 2: "Long thread", 3: "Big document", 4: "Changed mind",
         5: "Stays local", 6: "Forget", 7: "LinkedIn post", "dry": "Dry plumbing (no LLM)"}
FILLER_SEED = gen_history.SEED + 3
DRAFT_REQUEST = ("Write a short 5-sentence note to the team about the paint shade we picked for the office walls, "
                 "how we will arrange the storage room, our most-loved team ritual, and studying the results "
                 "of the customer survey.")
STATUS_REQUEST = "Write a 3-sentence status update about the website redesign."


def new_session() -> str:
    return str(uuid.uuid4())


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class Ctx:
    """What a scenario sees: send() with transcript, the DB path, failures and notes."""

    def __init__(self, core, config):
        self.core, self.config = core, config
        self.db = config.DB_PATH
        self.fails, self.notes, self.transcript = [], [], []

    def send(self, thread_id: str, text: str) -> str:
        t0 = time.monotonic()
        reply = self.core.send(USER, thread_id, text)
        self.transcript.append({"thread": thread_id, "user": text, "reply": reply,
                                "secs": round(time.monotonic() - t0, 1)})
        log(f"  [{thread_id[:8]}] > {text[:70]}\n  [{thread_id[:8]}] < {reply[:200]!r}")
        return reply

    def load_history(self, turns=None) -> int:
        t0 = time.monotonic()
        n = gen_history.load_history(USER, DM, turns=turns) if turns else gen_history.load_history(USER, DM)
        log(f"  loaded {n} messages into {DM} via core.ingest_turn in {time.monotonic() - t0:.1f}s")
        return n

    def fail(self, msg: str) -> None:
        self.fails.append(msg)

    def query(self, sql: str, args=()):
        import sqlite3
        c = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            return c.execute(sql, args).fetchall()
        finally:
            c.close()


def expect_in(x: Ctx, where: str, reply: str, needle: str) -> None:
    if checks.norm(needle) not in checks.norm(reply):
        x.fail(f"{where}: {needle!r} not in reply {reply[:160]!r}")


# ---- scenarios -----------------------------------------------------------------------------

def s1(x: Ctx):
    a, b = new_session(), new_session()
    x.send(b, "Hi! Opening this session now, I'll need some drafting help later.")
    x.send(a, "My manager is Priya Raman.")
    expect_in(x, "session B", x.send(b, "Who's my manager?"), "Priya Raman")
    expect_in(x, "slack-dm", x.send(DM, "Who's my manager?"), "Priya Raman")


def s2(x: Ctx):
    exp = gen_history.expected()
    x.load_history()
    expect_in(x, "cat (turn 20)", x.send(DM, "What's my cat's name again? I mentioned it a while back."), exp["cat_name"])
    expect_in(x, "restaurant (turn 22)", x.send(DM, "And what did I say my favourite restaurant was?"), exp["restaurant"])


def s3(x: Ctx):
    manifest = json.loads((ROOT / "docs" / "_manifest.json").read_text())
    launch = checks.parse_date(manifest["Q3-plan"]["facts"]["launch_date"])
    summary = x.send(DM, "Summarize doc Q3-plan.")
    if not summary.strip():
        x.fail("empty summary")
    # 100 filler turns through ingest_turn: same storage/memory path as live, no reply generation.
    x.load_history(gen_history.generate(100, seed=FILLER_SEED, plants=False))
    reply = x.send(DM, "What was the launch date in that plan?")
    found = checks.find_dates(reply)
    if not checks.mentions_date(reply, launch):
        x.fail(f"launch date {launch} not in reply; dates found {[str(d) for d in found]}: {reply[:200]!r}")
    others = sorted({str(d) for d in found if d != launch})
    if others:
        x.notes.append(f"other dates also in reply: {others}")


def _spelling(x: Ctx, where: str, text: str) -> None:
    brit, amer, words = checks.spelling_counts(text)
    x.notes.append(f"{where}: british={brit} american={amer}")
    if brit:
        x.fail(f"{where}: British forms after switch: {words}")
    if amer <= brit:
        x.fail(f"{where}: no American forms to judge (american={amer}); reply {text[:160]!r}")


def s4(x: Ctx):
    x.send(DM, "Always use British spelling.")
    brit, amer, _ = checks.spelling_counts(x.send(new_session(), DRAFT_REQUEST))
    x.notes.append(f"draft before switch: british={brit} american={amer}")
    x.send(DM, "Switch to American spelling.")
    _spelling(x, "new-session draft", x.send(new_session(), DRAFT_REQUEST))
    _spelling(x, "slack-dm draft", x.send(DM, DRAFT_REQUEST))
    before = x.send(DM, "What did I say about spelling before?")
    for word in ("British", "American"):
        expect_in(x, "history answer", before, word)


def s5(x: Ctx):
    a = new_session()
    x.send(a, STATUS_REQUEST)
    playful = x.send(a, "For this draft only, make it playful.")
    if not checks.playful_signals(playful):
        x.notes.append("weak proxy: session A's playful draft hit no playful signal")
    signals = checks.playful_signals(x.send(new_session(), STATUS_REQUEST))
    if signals:
        x.fail(f"session B draft looks playful: {signals}")
    rows = x.query("SELECT id, subject, value FROM facts WHERE scope = 'global'"
                   " AND (lower(subject) LIKE '%playful%' OR lower(value) LIKE '%playful%')")
    if rows:
        x.fail(f"global fact(s) mention playful: {rows}")


def s6(x: Ctx):
    exp = gen_history.expected()
    needles = [exp["address"], *exp["address_variants"]]
    x.load_history()
    if not checks.scan_db(x.db, needles):
        x.fail("positive control: address not in DB before forget (loader/plant broken)")
        return
    leaks = {"forget reply": checks.text_leaks(x.send(DM, "Forget my address."), needles)}
    leaks["partner reply"] = checks.text_leaks(x.send(DM, "Message my partner where I live."), needles)
    sent = x.query("SELECT recipient, text FROM send_log")
    x.notes.append(f"send_log rows={len(sent)} recipients={[r[0] for r in sent]}")
    leaks["send_log"] = [n for r in sent for n in checks.text_leaks(f"{r[0]} {r[1]}", needles)]
    leaks["db rows/fts"] = checks.scan_db(x.db, needles)
    leaks["raw files"] = checks.scan_files([*checks.db_files(x.db), x.config.CALL_LOG_PATH], needles)
    for where, hits in leaks.items():
        if hits:
            x.fail(f"address leaked in {where}: {hits[:5]}")


def s7(x: Ctx):
    x.load_history()
    reply = x.send(DM, "Write a LinkedIn post announcing our Q3 launch, based on doc Q3-plan.")
    post = extract_post(reply)
    errors = check_post(post)
    x.notes.append(f"post {len(post)} chars")
    if errors:
        x.fail(f"post rules: {errors}; post {post[:200]!r}")
    try:
        from chatbot.capabilities.linkedin import check_linkedin_post
        theirs = check_linkedin_post(post)
        if bool(theirs) != bool(errors):
            x.notes.append(f"checker disagreement: ours={errors} chatbot={theirs}")
    except ImportError as e:
        x.notes.append(f"chatbot checker not importable ({e}); own checker only")


SCENARIOS = {1: s1, 2: s2, 3: s3, 4: s4, 5: s5, 6: s6, 7: s7}


# ---- dry run (fake model, plumbing only) ---------------------------------------------------

def dry(x: Ctx):
    from chatbot import llm

    calls = []

    def create(**kw):
        calls.append(kw)
        msg = SimpleNamespace(content="ok", tool_calls=None)
        usage = SimpleNamespace(prompt_tokens=llm.estimate_tokens(kw["messages"], kw.get("tools")), completion_tokens=1)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)

    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    exp = gen_history.expected()
    needles = [exp["address"], *exp["address_variants"]]
    if gen_history.generate() != gen_history.generate():
        x.fail("generate() not deterministic")
    n = x.load_history()
    stored = x.query("SELECT COUNT(*) FROM messages WHERE thread_id = ?", (DM,))[0][0]
    if n != 2 * exp["n_turns"] or stored != n:
        x.fail(f"loaded {n}, stored {stored}, expected {2 * exp['n_turns']}")
    if not x.query("SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?", (exp["cat_name"],)):
        x.fail("FTS does not find the turn-20 detail")
    if not checks.scan_db(x.db, needles):
        x.fail("scan_db positive control: address not found")
    if not checks.scan_files(checks.db_files(x.db), needles):
        x.fail("scan_files positive control: address not in raw db/wal bytes")
    x.notes.append(f"model calls during 500-turn load: {len(calls)}")
    if not isinstance(x.send(DM, "ping"), str):
        x.fail("core.send did not return str")
    logged = x.query("SELECT COUNT(*) FROM llm_calls")[0][0]
    if logged != len(calls):
        x.fail(f"llm_calls rows {logged} != model calls {len(calls)}")
    x.notes.append(f"registered tools: {sorted(x.core.TOOLS)}")
    if not x.core.TOOLS:
        x.notes.append("WARN: core.TOOLS empty after import; S3/S6/S7 need tools registered on core import (T6)")
    try:
        from chatbot.capabilities.linkedin import check_linkedin_post  # noqa: F401
    except ImportError as e:
        x.notes.append(f"chatbot.capabilities.linkedin.check_linkedin_post missing: {e}")
    try:
        manifest = json.loads((ROOT / "docs" / "_manifest.json").read_text())
        x.notes.append(f"manifest launch_date={checks.parse_date(manifest['Q3-plan']['facts']['launch_date'])}")
    except (OSError, KeyError, ValueError) as e:
        x.fail(f"docs/_manifest.json Q3-plan launch_date unusable: {e!r}")


# ---- child / parent ------------------------------------------------------------------------

def budget(x: Ctx) -> dict:
    limit = x.config.MAX_INPUT_TOKENS
    rows = x.query("SELECT purpose, prompt_tokens, usage_missing, over_limit FROM llm_calls")
    over = [(p, t) for p, t, _, o in rows if t >= limit or o]
    missing = sum(1 for r in rows if r[2])
    if over:
        x.fail(f"{len(over)} call(s) at/over MAX_INPUT_TOKENS={limit}: {over[:5]}")
    if missing:
        x.fail(f"{missing} call(s) without provider usage (estimate logged instead)")
    return {"calls": len(rows), "max_prompt": max((r[1] for r in rows), default=0), "limit": limit}


def child(sid: str) -> int:
    if not os.environ.get("DB_PATH"):
        log("refusing to run: DB_PATH not set (run via python -m scenarios.run)")
        return 2
    from chatbot import config, core

    x = Ctx(core, config)
    try:
        (dry if sid == "dry" else SCENARIOS[int(sid)])(x)
    except Exception as e:
        log(traceback.format_exc())
        x.fail(f"crashed: {type(e).__name__}: {e}")
    stats = budget(x)
    Path(config.DB_PATH).with_name("transcript.json").write_text(json.dumps(x.transcript, indent=1))
    print(RESULT_TAG + json.dumps({"fails": x.fails, "notes": x.notes, **stats}), flush=True)
    return 0


def run_one(sid, keep: bool) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"scenario-s{sid}-"))
    env = {**os.environ, "DB_PATH": str(tmp / "chatbot.db"), "CALL_LOG_PATH": str(tmp / "calls.jsonl")}
    log(f"--- S{sid} {NAMES[sid]} (db {tmp})")
    p = subprocess.run([sys.executable, "-m", "scenarios.run", "--child", str(sid)],
                       cwd=ROOT, env=env, stdout=subprocess.PIPE, text=True)
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith(RESULT_TAG)]
    res = (json.loads(lines[-1][len(RESULT_TAG):]) if lines else
           {"fails": [f"child exited {p.returncode} without a result"], "notes": [], "calls": 0, "max_prompt": 0, "limit": None})
    if keep:
        res["notes"].append(f"kept {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scenarios.run", description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", help="comma-separated scenario numbers, e.g. 1,4")
    ap.add_argument("--keep-db", action="store_true", help="keep each scenario's temp DB, call log and transcript")
    ap.add_argument("--dry", action="store_true", help="no LLM: generate history, load it with a fake model, check plumbing")
    ap.add_argument("--child", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.child:
        return child(args.child)
    gen_history.write()
    ids = ["dry"] if args.dry else ([int(s) for s in args.only.split(",")] if args.only else list(SCENARIOS))
    bad = [i for i in ids if i != "dry" and i not in SCENARIOS]
    if bad:
        ap.error(f"unknown scenario(s): {bad}")
    results = {}
    for sid in ids:
        r = results[sid] = run_one(sid, args.keep_db)
        status = "FAIL" if r["fails"] else "PASS"
        reason = "; ".join(r["fails"]) or "ok"
        extra = f" | notes: {'; '.join(r['notes'])}" if r["notes"] else ""
        print(f"{status} S{sid} {NAMES[sid]} - {reason} [calls={r['calls']} max_prompt_tokens={r['max_prompt']}]{extra}",
              flush=True)
    passed = sum(1 for r in results.values() if not r["fails"])
    limit = next((r["limit"] for r in results.values() if r["limit"]), None)
    print(f"SUMMARY {passed}/{len(results)} passed; calls={sum(r['calls'] for r in results.values())}; "
          f"max prompt_tokens={max(r['max_prompt'] for r in results.values())} (limit {limit})")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
