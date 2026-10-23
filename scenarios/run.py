"""Scenario runner: S1-S7 against a fresh temp DB each, deterministic assertions, no LLM judge.

    python -m scenarios.run [--only 1,4] [--keep-db] [--dry]

Each scenario runs in its own child process with DB_PATH / CALL_LOG_PATH pointing at a temp dir,
set before chatbot is imported, so the repo's data/ and logs/ are never touched.
The 500-turn history is loaded ONCE per run into a snapshot dir; S2/S6/S7 (and --dry) start from a
copy.
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
NAMES = {
    1: "I just told you",
    2: "Long thread",
    3: "Big document",
    4: "Changed mind",
    5: "Stays local",
    6: "Forget",
    7: "LinkedIn post",
    "dry": "Dry plumbing (no LLM)",
    "snapshot": "History snapshot (500 turns, loaded once)",
}
HISTORY_SCENARIOS = {2, 6, 7, "dry"}  # start from a copy of the snapshot instead of loading again
PRELOADED_ENV = "SCENARIO_PRELOADED"
FAKE_LLM_ENV = "SCENARIO_FAKE_LLM"
FILLER_SEED = gen_history.SEED + 3
DRAFT_REQUEST = (
    "Write a short 5-sentence note to the team about the paint shade we picked for the office "
    "walls, "
    "how we will arrange the storage room, our most-loved team ritual, and studying the results "
    "of the customer survey."
)
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
        self.preloaded = os.environ.get(PRELOADED_ENV) == "1"
        from chatbot import db

        # rows copied from the snapshot belong to the snapshot's own budget line, not this
        # scenario's
        self.calls_base = (
            db.conn().execute("SELECT COALESCE(MAX(id), 0) FROM llm_calls").fetchone()[0]
        )

    def send(self, thread_id: str, text: str) -> str:
        t0 = time.monotonic()
        reply = self.core.send(USER, thread_id, text)
        self.transcript.append(
            {
                "thread": thread_id,
                "user": text,
                "reply": reply,
                "secs": round(time.monotonic() - t0, 1),
            }
        )
        log(f"  [{thread_id[:8]}] > {text[:70]}\n  [{thread_id[:8]}] < {reply[:200]!r}")
        return reply

    def load_history(self, turns=None) -> int:
        if turns is None and self.preloaded:
            n = self.query(
                "SELECT COUNT(*) FROM messages WHERE user_id = ? AND thread_id = ?", (USER, DM)
            )[0][0]
            log(f"  history: {n} messages in {DM} from the snapshot copy")
            return n
        t0 = time.monotonic()
        n = (
            gen_history.load_history(USER, DM, turns=turns)
            if turns
            else gen_history.load_history(USER, DM)
        )
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
    # W4: a rephrased statement first (no regex-friendly "my manager is"), then the spec phrasing,
    # which must supersede it. Each pair: B is opened before A states the fact.
    for statement, name in (
        ("Heads up: Wilhelmina Strand is my boss now.", "Wilhelmina Strand"),
        ("My manager is Priya Raman.", "Priya Raman"),
    ):
        a, b = new_session(), new_session()
        x.send(b, "Hi! Opening this session now, I'll need some drafting help later.")
        x.send(a, statement)
        expect_in(x, f"session B ({name})", x.send(b, "Who's my manager?"), name)
    expect_in(x, "slack-dm", x.send(DM, "Who's my manager?"), "Priya Raman")


def s2(x: Ctx):
    exp = gen_history.expected()
    x.load_history()
    expect_in(
        x,
        "cat (turn 20)",
        x.send(DM, "What's my cat's name again? I mentioned it a while back."),
        exp["cat_name"],
    )
    expect_in(
        x,
        "restaurant (turn 22)",
        x.send(DM, "And what did I say my favourite restaurant was?"),
        exp["restaurant"],
    )


def q3_launch():
    manifest = json.loads((ROOT / "docs" / "_manifest.json").read_text())
    return checks.parse_date(manifest["Q3-plan"]["facts"]["launch_date"])


def q3_distractors(launch):
    """Every other full date written in the doc itself (drafts, milestones, embargo...)."""
    return sorted(set(checks.find_dates((ROOT / "docs" / "Q3-plan.md").read_text())) - {launch})


def check_launch_answer(reply: str, launch, distractors) -> list[str]:
    """W3: the true launch date is stated and no distractor date is (with or without year)."""
    errors = []
    if not checks.mentions_date(reply, launch):
        errors.append(
            f"launch date {launch} not in reply; dates found "
            f"{[str(d) for d in checks.find_dates(reply)]}"
        )
    wrong = [str(d) for d in distractors if checks.mentions_date(reply, d)]
    if wrong:
        errors.append(f"distractor date(s) in reply: {wrong}")
    return [f"{e}: {reply[:200]!r}" for e in errors]


def s3(x: Ctx):
    launch = q3_launch()
    summary = x.send(DM, "Summarize doc Q3-plan.")
    if not summary.strip():
        x.fail("empty summary")
    # 100 filler turns through ingest_turn: same storage/memory path as live, no reply generation.
    x.load_history(gen_history.generate(100, seed=FILLER_SEED, plants=False))
    for e in check_launch_answer(
        x.send(DM, "What was the launch date in that plan?"), launch, q3_distractors(launch)
    ):
        x.fail(e)


def check_spelling(where: str, text: str, want: str) -> list[str]:
    """want='british' | 'american': at least one form of `want` and none of the other."""
    brit, amer, words = checks.spelling_counts(text)
    good, bad = (brit, amer) if want == "british" else (amer, brit)
    errors = []
    if bad:
        errors.append(f"{where}: {bad} non-{want} form(s) (british words: {words})")
    if not good:
        errors.append(f"{where}: no {want} forms to judge; reply {text[:160]!r}")
    return errors


def check_spelling_history(text: str) -> list[str]:
    return [
        f"history answer: {w!r} not in reply {text[:160]!r}"
        for w in ("British", "American")
        if checks.norm(w) not in checks.norm(text)
    ]


def statements_in_window(x: Ctx, needle: str) -> list[str]:
    """DM messages the model sees verbatim (last RECENT_TURNS) that contain needle."""
    rows = x.query(
        "SELECT content FROM messages WHERE user_id = ? AND thread_id = ? ORDER BY id DESC LIMIT ?",
        (USER, DM, x.core.RECENT_TURNS),
    )
    return [r[0] for r in rows if needle in r[0].lower()]


def window_filler(recent_messages: int) -> int:
    """Filler turns (2 messages each) that push a statement out of a recent window of that many
    messages."""
    return recent_messages // 2 + 5


def s4(x: Ctx):
    # W1: push each spelling statement out of DM's recent window, so only memory can carry it.
    filler = window_filler(x.core.RECENT_TURNS)
    x.send(DM, "Always use British spelling.")
    x.load_history(gen_history.generate(filler, seed=FILLER_SEED, plants=False))
    for e in check_spelling(
        "pre-switch new-session draft", x.send(new_session(), DRAFT_REQUEST), "british"
    ):
        x.fail(e)
    x.send(DM, "Switch to American spelling.")
    x.load_history(gen_history.generate(filler, seed=FILLER_SEED + 1, plants=False))
    for where, thread in (("new-session draft", new_session()), ("slack-dm draft", DM)):
        for e in check_spelling(where, x.send(thread, DRAFT_REQUEST), "american"):
            x.fail(e)
    leaked = statements_in_window(x, "spelling")
    if leaked:
        x.fail(f"harness: spelling statement still in the recent window: {leaked}")
    for e in check_spelling_history(x.send(DM, "What did I say about spelling before?")):
        x.fail(e)


def s5(x: Ctx):
    a = new_session()
    x.send(a, STATUS_REQUEST)
    playful = x.send(a, "For this draft only, make it playful.")
    if not checks.playful_signals(playful):
        x.notes.append("weak proxy: session A's playful draft hit no playful signal")
    signals = checks.playful_signals(x.send(new_session(), STATUS_REQUEST))
    if signals:
        x.fail(f"session B draft looks playful: {signals}")
    rows = x.query(
        "SELECT id, subject, value FROM facts WHERE scope = 'global'"
        " AND (lower(subject) LIKE '%playful%' OR lower(value) LIKE '%playful%')"
    )
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
    leaks["partner reply"] = checks.text_leaks(
        x.send(DM, "Message my partner where I live."), needles
    )
    sent = x.query("SELECT recipient, text FROM send_log")
    x.notes.append(f"send_log rows={len(sent)} recipients={[r[0] for r in sent]}")
    leaks["send_log"] = [n for r in sent for n in checks.text_leaks(f"{r[0]} {r[1]}", needles)]
    leaks["db rows/fts"] = checks.scan_db(x.db, needles)
    leaks["raw files"] = checks.scan_files(
        [*checks.db_files(x.db), x.config.CALL_LOG_PATH], needles
    )
    for where, hits in leaks.items():
        if hits:
            x.fail(f"address leaked in {where}: {hits[:5]}")


MIN_POST_CHARS = 200


def check_linkedin_reply(reply: str, launch) -> list[str]:
    """W2: a real post (not an error or refusal), long enough, with the launch date, passing the
    rules."""
    post = extract_post(reply)
    head = (reply or "").strip().lower()
    errors = []
    if head.startswith("error") or not post:
        errors.append("error reply or no extractable post")
    if len(post) < MIN_POST_CHARS:
        errors.append(f"post too short: {len(post)} < {MIN_POST_CHARS} chars")
    if not checks.mentions_date(post, launch):
        errors.append(f"launch date {launch} not in post")
    rules = check_post(post)
    if rules:
        errors.append(f"post rules: {rules}")
    return [f"{e}; post {post[:200]!r}" for e in errors]


def s7(x: Ctx):
    x.load_history()
    reply = x.send(DM, "Write a LinkedIn post announcing our Q3 launch, based on doc Q3-plan.")
    post = extract_post(reply)
    errors = check_post(post)
    x.notes.append(f"post {len(post)} chars")
    for e in check_linkedin_reply(reply, q3_launch()):
        x.fail(e)
    try:
        from chatbot.capabilities.linkedin import check_linkedin_post

        theirs = check_linkedin_post(post)
        if bool(theirs) != bool(errors):
            x.notes.append(f"checker disagreement: ours={errors} chatbot={theirs}")
    except ImportError as e:
        x.notes.append(f"chatbot checker not importable ({e}); own checker only")


SCENARIOS = {1: s1, 2: s2, 3: s3, 4: s4, 5: s5, 6: s6, 7: s7}


# ---- dry run (fake model, plumbing only) ---------------------------------------------------

FAKE_CALLS: list = []


def install_fake_model() -> None:
    from chatbot import llm

    def create(**kw):
        FAKE_CALLS.append(kw)
        msg = SimpleNamespace(
            content='{"facts": []}', tool_calls=None
        )  # valid JSON: no ask_json retry
        usage = SimpleNamespace(
            prompt_tokens=llm.estimate_tokens(kw["messages"], kw.get("tools")), completion_tokens=1
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)

    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def snapshot(x: Ctx):
    """Load the 500-turn history once; scenarios in HISTORY_SCENARIOS get a copy of this dir."""
    x.load_history()
    by_purpose = x.query(
        "SELECT purpose, COUNT(*) FROM llm_calls GROUP BY purpose ORDER BY purpose"
    )
    x.notes.append("load calls by purpose: " + ", ".join(f"{p}={n}" for p, n in by_purpose))


def dry(x: Ctx):
    """Runs on a snapshot copy loaded with the fake model: checks the load and the copy plumbing."""
    exp = gen_history.expected()
    needles = [exp["address"], *exp["address_variants"]]
    if gen_history.generate() != gen_history.generate():
        x.fail("generate() not deterministic")
    if not x.preloaded or not x.calls_base:
        x.fail(
            f"expected a snapshot copy with load calls (preloaded={x.preloaded}, "
            f"calls_base={x.calls_base})"
        )
    n = x.load_history()
    stored = x.query("SELECT COUNT(*) FROM messages WHERE thread_id = ?", (DM,))[0][0]
    if n != 2 * exp["n_turns"] or stored != n:
        x.fail(f"loaded {n}, stored {stored}, expected {2 * exp['n_turns']}")
    extracts = x.query("SELECT COUNT(*) FROM llm_calls WHERE purpose = 'extract'")[0][0]
    if extracts != exp["n_turns"]:  # D-2: one extract call per user turn on the loader path too
        x.fail(
            f"history load made {extracts} extract calls, expected {exp['n_turns']} (one per user "
            "turn)"
        )
    if not x.query("SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?", (exp["cat_name"],)):
        x.fail("FTS does not find the turn-20 detail")
    if not checks.scan_db(x.db, needles):
        x.fail("scan_db positive control: address not found")
    if not checks.scan_files(checks.db_files(x.db), needles):
        x.fail("scan_files positive control: address not in raw db/wal bytes")
    if not isinstance(x.send(DM, "ping"), str):
        x.fail("core.send did not return str")
    logged = x.query("SELECT COUNT(*) FROM llm_calls WHERE id > ?", (x.calls_base,))[0][0]
    if logged != len(FAKE_CALLS):
        x.fail(f"llm_calls rows {logged} != model calls {len(FAKE_CALLS)}")
    x.notes.append(f"registered tools: {sorted(x.core.TOOLS)}")
    if not x.core.TOOLS:
        x.notes.append(
            "WARN: core.TOOLS empty after import; S3/S6/S7 need tools registered on core import "
            "(T6)"
        )
    try:
        from chatbot.capabilities.linkedin import check_linkedin_post  # noqa: F401
    except ImportError as e:
        x.notes.append(f"chatbot.capabilities.linkedin.check_linkedin_post missing: {e}")
    try:
        manifest = json.loads((ROOT / "docs" / "_manifest.json").read_text())
        x.notes.append(
            f"manifest launch_date={checks.parse_date(manifest['Q3-plan']['facts']['launch_date'])}"
        )
    except (OSError, KeyError, ValueError) as e:
        x.fail(f"docs/_manifest.json Q3-plan launch_date unusable: {e!r}")


# ---- child / parent ------------------------------------------------------------------------


def budget(x: Ctx) -> dict:
    limit = x.config.MAX_INPUT_TOKENS
    rows = x.query(
        "SELECT purpose, prompt_tokens, usage_missing, over_limit FROM llm_calls WHERE id > ?",
        (x.calls_base,),
    )
    over = [(p, t) for p, t, _, o in rows if t >= limit or o]
    missing = sum(1 for r in rows if r[2])
    if over:
        x.fail(f"{len(over)} call(s) at/over MAX_INPUT_TOKENS={limit}: {over[:5]}")
    if missing:
        x.fail(f"{missing} call(s) without provider usage (estimate logged instead)")
    if not rows:
        x.fail("no model calls logged: the budget gate checked nothing")
    return {"calls": len(rows), "max_prompt": max((r[1] for r in rows), default=0), "limit": limit}


def child(sid: str) -> int:
    if not os.environ.get("DB_PATH"):
        log("refusing to run: DB_PATH not set (run via python -m scenarios.run)")
        return 2
    from chatbot import config, core

    if os.environ.get(FAKE_LLM_ENV) == "1":
        install_fake_model()
    x = Ctx(core, config)
    try:
        {"dry": dry, "snapshot": snapshot}.get(sid, lambda c: SCENARIOS[int(sid)](c))(x)
    except Exception as e:
        log(traceback.format_exc())
        x.fail(f"crashed: {type(e).__name__}: {e}")
    stats = budget(x)
    Path(config.DB_PATH).with_name("transcript.json").write_text(json.dumps(x.transcript, indent=1))
    print(RESULT_TAG + json.dumps({"fails": x.fails, "notes": x.notes, **stats}), flush=True)
    return 0


def run_one(sid, keep: bool, snap: Path | None = None, fake: bool = False) -> dict:
    """snap: a finished snapshot dir copied into this scenario's temp dir (DB, WAL, call log,
    forget.key)."""
    tmp = Path(tempfile.mkdtemp(prefix=f"scenario-s{sid}-"))
    env = {
        **os.environ,
        "DB_PATH": str(tmp / "chatbot.db"),
        "CALL_LOG_PATH": str(tmp / "calls.jsonl"),
        PRELOADED_ENV: "1" if snap else "0",
        FAKE_LLM_ENV: "1" if fake else "0",
    }
    if snap:
        shutil.copytree(
            snap, tmp, dirs_exist_ok=True, ignore=shutil.ignore_patterns("transcript.json")
        )
    log(f"--- S{sid} {NAMES[sid]} (db {tmp}{', snapshot copy' if snap else ''})")
    p = subprocess.run(
        [sys.executable, "-m", "scenarios.run", "--child", str(sid)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith(RESULT_TAG)]
    res = (
        json.loads(lines[-1][len(RESULT_TAG) :])
        if lines
        else {
            "fails": [f"child exited {p.returncode} without a result"],
            "notes": [],
            "calls": 0,
            "max_prompt": 0,
            "limit": None,
        }
    )
    res["dir"] = tmp
    if keep:
        res["notes"].append(f"kept {tmp}")
    elif sid != "snapshot":  # main() removes the snapshot after the last copy
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m scenarios.run", description=__doc__.split("\n\n")[0]
    )
    ap.add_argument("--only", help="comma-separated scenario numbers, e.g. 1,4")
    ap.add_argument(
        "--keep-db",
        action="store_true",
        help="keep each scenario's temp DB, call log and transcript",
    )
    ap.add_argument(
        "--dry",
        action="store_true",
        help="no LLM: generate history, load it with a fake model, check plumbing",
    )
    ap.add_argument("--child", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.child:
        return child(args.child)
    gen_history.write()
    ids = (
        ["dry"]
        if args.dry
        else ([int(s) for s in args.only.split(",")] if args.only else list(SCENARIOS))
    )
    bad = [i for i in ids if i != "dry" and i not in SCENARIOS]
    if bad:
        ap.error(f"unknown scenario(s): {bad}")
    results, snap = {}, None
    if HISTORY_SCENARIOS & set(ids):
        ids = ["snapshot", *ids]
    for sid in ids:
        r = results[sid] = run_one(
            sid, args.keep_db, snap if sid in HISTORY_SCENARIOS else None, args.dry
        )
        if sid == "snapshot":
            snap = r["dir"]
        status = "FAIL" if r["fails"] else "PASS"
        reason = "; ".join(r["fails"]) or "ok"
        extra = f" | notes: {'; '.join(r['notes'])}" if r["notes"] else ""
        print(
            f"{status} S{sid} {NAMES[sid]} - {reason} [calls={r['calls']} "
            f"max_prompt_tokens={r['max_prompt']}]{extra}",
            flush=True,
        )
    passed = sum(1 for r in results.values() if not r["fails"])
    limit = next((r["limit"] for r in results.values() if r["limit"]), None)
    print(
        f"SUMMARY {passed}/{len(results)} passed; "
        f"calls={sum(r['calls'] for r in results.values())}; "
        f"max prompt_tokens={max(r['max_prompt'] for r in results.values())} (limit {limit})"
    )
    if snap and not args.keep_db:
        shutil.rmtree(snap, ignore_errors=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
