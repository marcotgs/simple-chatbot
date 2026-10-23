"""R3-C1 live: uploaded numeric CSV and long prose doc read through the real model.

Slack-faithful: slack_app.handle_message with only the Slack download faked (same seam as
test_integration_flows.SlackFlowTest), so the file note, core.send, tool loop, gate, extractor and
doc notes all run for real. Plus a pasted ~15k-char numeric CSV through core.send. Every request
must come back with an answer (no BudgetExceeded / "too long" reply) and every logged provider
prompt_tokens must stay < MAX_INPUT_TOKENS. Spends ~$0.05 on gpt-4.1-mini, so opt-in:
    RUN_LIVE_TESTS=1 .venv/bin/python -m unittest tests.test_live_docs_upload -v"""

import os
import re
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "live-docs.db"))
os.environ.setdefault("CALL_LOG_PATH", os.path.join(_tmp, "calls.jsonl"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatbot import config, core, db, llm, slack_app, uploads  # noqa: E402
from chatbot.capabilities import docs  # noqa: E402
from chatbot.memory import doc_notes  # noqa: E402

LIVE = (
    os.environ.get("RUN_LIVE_TESTS") == "1"
    and bool(config.OPENROUTER_API_KEY)
    and not config.OPENROUTER_API_KEY.startswith("test-")
)
OWNER = "U_OWNER_R4"
TOO_LONG = "too long for my token budget"
WENT_WRONG = "Something went wrong"
# USD per 1M tokens (input, output); OpenRouter list prices 2026-10.
PRICE = {"openai/gpt-4.1-mini": (0.40, 1.60), "typesafe/jev-1.14": (0.042, 0.042)}


def sales_csv() -> str:
    rows = ["date,region,units,revenue,cost"] + [
        f"2026-{r % 12 + 1:02d}-{r % 28 + 1:02d},R{r % 7},{(r * 37) % 1000},"
        f"{(r * 7919) % 100000}.{r % 100:02d},{(r * 104729) % 99991}.50"
        for r in range(2000)
    ]
    return "\n".join(rows)


def handbook() -> str:
    paras = [
        f"Section {i}. The onboarding team reviews access requests for the regional offices "
        f"every week, records the decision in the ledger and notifies the requester by email. "
        f"Reference code for section {i}: HB-{(i * 7919) % 100000:05d}. Escalations go to the "
        f"office lead, who answers within two working days and closes the ticket."
        for i in range(1, 121)
    ]
    return "\n\n".join(paras)


def calls_since(last_id: int) -> list:
    return (
        db.conn()
        .execute(
            "SELECT purpose, model, estimated_tokens, prompt_tokens, completion_tokens,"
            " usage_missing, over_limit FROM llm_calls WHERE id > ? ORDER BY id",
            (last_id,),
        )
        .fetchall()
    )


def last_call_id() -> int:
    return db.conn().execute("SELECT COALESCE(MAX(id), 0) FROM llm_calls").fetchone()[0]


def cost(calls) -> float:
    total = 0.0
    for c in calls:
        pin, pout = PRICE.get(c["model"], (0.40, 1.60))
        total += (c["prompt_tokens"] * pin + (c["completion_tokens"] or 0) * pout) / 1e6
    return total


@unittest.skipUnless(LIVE, "live: set RUN_LIVE_TESTS=1 with a real OPENROUTER_API_KEY")
class LiveDocsUploadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved, llm._client = llm._client, None  # real OpenRouter client

    @classmethod
    def tearDownClass(cls):
        llm._client = cls._saved

    def setUp(self):
        self.user = f"r4live-{uuid.uuid4().hex[:8]}"
        self.files, self.said, self.tools, self.n = {}, [], [], 0
        real_run_tool = core._run_tool

        def spy(tools, tc, user_id, thread_id):
            self.tools.append(tc.function.name)
            return real_run_tool(tools, tc, user_id, thread_id)

        for p in (
            mock.patch.object(docs, "UPLOADS_DIR", Path(tempfile.mkdtemp())),
            mock.patch.object(slack_app, "USER_MAP", {OWNER: self.user}),
            mock.patch.object(uploads, "download", lambda url, token, max_bytes=0: self.files[url]),
            mock.patch.object(core, "_run_tool", spy),
        ):
            p.start()
            self.addCleanup(p.stop)
        slack_app._seen.clear()
        slack_app._seen_order.clear()

    def dm(self, text, files=()) -> tuple[str, float, list]:
        """One Slack DM through handle_message. Returns (reply, seconds, llm_calls rows)."""
        self.n += 1
        ev = {
            "type": "message",
            "channel_type": "im",
            "channel": "D1",
            "user": OWNER,
            "text": text,
            "ts": f"1800000000.{self.n:06d}",
            "client_msg_id": uuid.uuid4().hex,
        }
        if files:
            ev["subtype"], ev["files"] = "file_share", []
            for name, data in files:
                url = f"https://files.slack.com/files-pri/T1-F{self.n}/{name}"
                self.files[url] = data
                ev["files"].append(
                    {
                        "id": f"F{self.n}",
                        "name": name,
                        "size": len(data),
                        "url_private_download": url,
                    }
                )
        self.said.clear()
        before, t0 = last_call_id(), time.monotonic()
        slack_app.handle_message(ev, self.said.append)
        return self.said[-1], time.monotonic() - t0, calls_since(before)

    def assert_answered_under_max(self, label, reply, secs, calls):
        chat = [c for c in calls if c["purpose"] == "chat"]
        print(
            f"\n[R4 {label}] {secs:.1f}s calls={len(calls)} chat={len(chat)}"
            f" max_prompt={max(c['prompt_tokens'] for c in calls)}"
            f" max_est={max(c['estimated_tokens'] for c in calls)}"
            f" purposes={sorted({c['purpose'] for c in calls})} cost=${cost(calls):.4f}"
            f" tools={self.tools}\n  reply: {reply[:300]!r}",
            file=sys.stderr,
        )
        self.assertNotIn(TOO_LONG, reply)
        self.assertNotIn(WENT_WRONG, reply)
        self.assertTrue(chat)
        self.assertLess(max(c["prompt_tokens"] for c in calls), config.MAX_INPUT_TOKENS)
        self.assertFalse(any(c["over_limit"] for c in calls))

    def test_uploaded_csv_and_prose_summarized_and_page_3_fetched(self):
        csv, prose = sales_csv(), handbook()
        reply, secs, calls = self.dm(
            "Summarize sales.csv for me.",
            files=[("sales.csv", csv.encode()), ("handbook.txt", prose.encode())],
        )
        ack = self.said[0]
        csv_doc, txt_doc = "upload-sales", "upload-handbook"
        csv_pages, txt_pages = (
            docs.page_count(csv_doc, self.user),
            docs.page_count(txt_doc, self.user),
        )
        print(
            f"\n[R4 pages] sales.csv {len(csv)} chars -> {csv_pages} pages;"
            f" handbook.txt {len(prose)} chars -> {txt_pages} pages\n  ack: {ack!r}",
            file=sys.stderr,
        )
        self.assertIn(f"{csv_pages} page(s)", ack)
        self.assert_answered_under_max("csv summary", reply, secs, calls)
        self.assertIn("summarize_doc", self.tools)
        # every CSV page noted (none failed the budget); the model may summarize the txt too
        self.assertEqual(
            doc_notes.noted_pages(self.user, slack_app.THREAD_ID, csv_doc),
            set(range(1, csv_pages + 1)),
        )

        self.tools.clear()
        first_row = docs.read_page(csv_doc, 3, self.user).lstrip().splitlines()[0]
        revenue = first_row.split(",")[3]
        reply, secs, calls = self.dm(
            f"Use fetch_doc on document {csv_doc} page 3 and tell me the revenue on the first"
            " row of that page."
        )
        self.assert_answered_under_max("csv page 3", reply, secs, calls)
        self.assertIn("fetch_doc", self.tools)
        self.assertIn(revenue.split(".")[0], reply.replace(",", ""))

        self.tools.clear()
        code = re.search(r"HB-\d{5}", docs.read_page(txt_doc, 3, self.user)).group()
        reply, secs, calls = self.dm(
            f"Use fetch_doc on document {txt_doc} page 3 and quote the first reference code on it."
        )
        self.assert_answered_under_max("prose page 3", reply, secs, calls)
        self.assertIn("fetch_doc", self.tools)
        self.assertIn(code, reply)

    def test_page_3_read_whole_after_24_turn_thread(self):
        """R4-C1: in a long chat the newest fetch_doc result reached the model as 300 chars + mark.
        Seeds 24 prior turns on the Slack thread (same rows core.ingest_turn writes, without 24 live
        extraction calls), uploads a doc, asks for page 3 and checks the last chat payload."""
        topics = ["the office move", "the hiring plan", "vendor contracts", "the travel policy"]
        with db.conn() as c:
            for t in range(1, 25):
                role = "user" if t % 2 else "assistant"
                text = (
                    f"Turn {t} about {topics[t % 4]}: "
                    + "we went through the open questions, owners and dates again in detail. " * 8
                )
                c.execute(
                    "INSERT INTO messages (user_id, thread_id, role, content, turn_no)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (self.user, slack_app.THREAD_ID, role, text, t),
                )
        plan = handbook().replace("onboarding team", "Q3 planning group").replace("HB-", "QP-")
        self.dm("Here is the Q3 plan.", files=[("Q3-plan.txt", plan.encode())])
        doc = "upload-Q3-plan"
        self.assertGreaterEqual(docs.page_count(doc, self.user), 4)
        page3 = docs.read_page(doc, 3, self.user)
        last_code = re.findall(r"QP-\d{5}", page3)[-1]  # deep in the page: past any 300-char cut

        payloads, real_chat = [], llm.chat

        def chat_spy(messages, tools=None, purpose="chat", **kw):
            if purpose == "chat":
                payloads.append(messages)
            return real_chat(messages, tools, purpose=purpose, **kw)

        # 1) the user's own wording (the model picks the tool); 2) fetch_doc named, the R4-C1 path.
        for label, ask in (
            ("natural", "What is on page 3 of Q3-plan.txt? Quote the last reference code on it."),
            (
                "fetch_doc",
                f"Use fetch_doc on document {doc} page 3 and quote its last reference code.",
            ),
        ):
            with self.subTest(label):
                self.tools.clear()
                payloads.clear()
                with mock.patch.object(llm, "chat", chat_spy):
                    reply, secs, calls = self.dm(ask)
                final = payloads[-1]
                newest = [m for m in final if m["role"] == "tool"][-1:]
                print(
                    f"\n[R4-C1 {label}] payload msgs={len(final)}"
                    f" trimmed={core.TRIM_NOTE in final[0]['content']}"
                    f" tool_chars={[len(m['content']) for m in newest]} page_chars={len(page3)}"
                    f" last_code={last_code}",
                    file=sys.stderr,
                )
                self.assert_answered_under_max(f"page 3 after 24 turns {label}", reply, secs, calls)
                self.assertTrue(newest, "no tool result in the final chat payload")
                self.assertNotIn(core.TOOL_SHORTENED_MARK, newest[0]["content"])
                # B3: plain words must also reach fetch_doc (not summarize_doc notes)
                self.assertIn("fetch_doc", self.tools)
                self.assertIn(page3, newest[0]["content"])
                self.assertIn(last_code, reply)

    def test_pasted_15k_numeric_csv_gets_an_answer_under_max(self):
        table = "\n".join(
            f"sensor{r % 9},{(r * 37) % 1000 / 10:.1f},{(r * 7919) % 100000},"
            f"{(r * 104729) % 997 / 1000:.3f}"
            for r in range(600)
        )
        self.assertGreater(len(table), 14000)
        reply, secs, calls = self.dm("Temperature log, how many sensors are listed?\n" + table)
        self.assert_answered_under_max("pasted csv", reply, secs, calls)


if __name__ == "__main__":
    unittest.main()
