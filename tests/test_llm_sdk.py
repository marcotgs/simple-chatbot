"""llm.chat through the real OpenRouter SDK client, with a fake HTTP transport (no network).

Proves the request body llm.chat sends (model, messages, tools, max_tokens) and that the SDK
response comes back in the shape core/memory read (choices[0].message, usage).
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ.update(
    {
        "DB_PATH": os.path.join(_tmp, "test.db"),
        "CALL_LOG_PATH": os.path.join(_tmp, "calls.jsonl"),
        "GATE_MODEL": "",
        "OPENROUTER_API_KEY": "test-not-a-key",
    }
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from openrouter import errors  # noqa: E402
from openrouter.utils import unmarshal_json  # noqa: E402
from openrouter.utils.logger import NoOpLogger  # noqa: E402

from chatbot import config, db, llm  # noqa: E402

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": "Search docs.",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }
]
# A tool round trip as core.run_tool_loop builds it: assistant tool_calls + tool result.
MESSAGES = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "find the plan"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_0",
                "type": "function",
                "function": {"name": "search_docs", "arguments": '{"q": "plan"}'},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "call_0", "content": "Q3-plan p1"},
]


def completion(message: dict, usage: dict | None) -> dict:
    body = {
        "id": "gen-1",
        "object": "chat.completion",
        "created": 1,
        "model": "openai/gpt-4.1-mini",  # no system_fingerprint: documented as optional
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


class SdkClientTest(unittest.TestCase):
    def setUp(self):
        self.requests: list[httpx.Request] = []
        self.responses: list[httpx.Response] = []
        real_client = httpx.Client

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.responses.pop(0)

        transport = httpx.MockTransport(handler)
        patch = mock.patch.object(
            httpx, "Client", lambda **kw: real_client(transport=transport, **kw)
        )
        patch.start()
        self.addCleanup(patch.stop)
        saved, llm._client = llm._client, None  # llm builds the real SDK client
        self.addCleanup(setattr, llm, "_client", saved)

    def test_tool_call_request_and_response(self):
        self.responses.append(
            httpx.Response(
                200,
                json=completion(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "search_docs", "arguments": '{"q": "x"}'},
                            }
                        ],
                    },
                    {"prompt_tokens": 321, "completion_tokens": 7, "total_tokens": 328},
                ),
            )
        )
        resp = llm.chat(MESSAGES, TOOLS, purpose="doc_note", thread_id="t-sdk-1", model="m/x")

        (req,) = self.requests
        self.assertEqual(str(req.url), config.OPENROUTER_BASE_URL + "/chat/completions")
        self.assertEqual(req.headers["authorization"], "Bearer test-not-a-key")
        body = json.loads(req.content)
        self.assertEqual(body["model"], "m/x")
        self.assertEqual(body["messages"], MESSAGES)
        self.assertEqual(body["tools"], TOOLS)
        self.assertEqual(body["max_tokens"], config.DOC_NOTE_MAX_TOKENS)

        msg = resp.choices[0].message
        self.assertEqual(msg.content, None)
        (tc,) = msg.tool_calls
        self.assertEqual((tc.id, tc.function.name), ("call_1", "search_docs"))
        self.assertEqual(json.loads(tc.function.arguments), {"q": "x"})
        row = (
            db.conn()
            .execute(
                "SELECT prompt_tokens, completion_tokens, usage_missing FROM llm_calls"
                " WHERE thread_id = 't-sdk-1'"
            )
            .fetchone()
        )
        self.assertEqual(tuple(row), (321, 7, 0))

    def test_plain_reply_without_tools_or_cap(self):
        self.responses.append(
            httpx.Response(200, json=completion({"role": "assistant", "content": "hi"}, None))
        )
        resp = llm.chat([{"role": "user", "content": "hello"}], thread_id="t-sdk-2")

        body = json.loads(self.requests[0].content)
        self.assertEqual(body["model"], config.LLM_MODEL)
        self.assertNotIn("tools", body)
        self.assertNotIn("max_tokens", body)
        self.assertEqual(resp.choices[0].message.content, "hi")
        self.assertIsNone(resp.choices[0].message.tool_calls)
        row = (
            db.conn()
            .execute("SELECT usage_missing FROM llm_calls WHERE thread_id = 't-sdk-2'")
            .fetchone()
        )
        self.assertEqual(row[0], 1)  # no usage: estimate stands in, flagged

    def test_content_omitted_reads_as_none(self):
        self.responses.append(
            httpx.Response(
                200,
                json=completion(
                    {"role": "assistant"},
                    {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5},
                ),
            )
        )
        resp = llm.chat([{"role": "user", "content": "hello"}])
        self.assertIsNone(resp.choices[0].message.content)

    def test_client_error_propagates(self):
        self.responses.append(
            httpx.Response(400, json={"error": {"code": 400, "message": "tools not supported"}})
        )
        with self.assertRaises(Exception) as ctx:
            llm.chat([{"role": "user", "content": "hello"}], TOOLS)
        self.assertNotIn("test-not-a-key", str(ctx.exception))
        self.assertEqual(len(self.requests), 1)  # 4xx other than 408/429: no retry

    def test_client_is_bounded(self):
        client = llm._get_client()
        cfg = client.sdk_configuration
        self.assertEqual(cfg.timeout_ms, llm.TIMEOUT_MS)
        self.assertLessEqual(cfg.retry_config.backoff.max_elapsed_time, 30_000)
        self.assertIn("429", cfg.retry_config.status_codes_override)

    def test_optional_fields_missing_still_parse(self):
        body = completion({"role": "assistant", "content": "hi"}, None)
        for key in ("finish_reason", "index"):
            del body["choices"][0][key]
        self.responses.append(httpx.Response(200, json=body))
        resp = llm.chat([{"role": "user", "content": "hello"}])
        self.assertEqual(resp.choices[0].message.content, "hi")
        self.assertIsNone(resp.choices[0].finish_reason)

    def test_broken_body_still_raises(self):
        body = completion({"role": "assistant", "content": "hi"}, None)
        del body["created"]  # required and not defaulted: no silent repair
        self.responses.append(httpx.Response(200, json=body))
        with self.assertRaises(errors.ResponseValidationError):
            llm.chat([{"role": "user", "content": "hello"}])

    def test_non_200_body_is_never_reparsed(self):
        # A 400 whose body is not the error schema but looks like a completion once defaults are
        # filled: it must surface as the SDK's error, never as a ChatResult.
        body = completion({"role": "assistant", "content": "hi"}, None)
        self.responses.append(httpx.Response(400, json=body))
        with (
            mock.patch("openrouter.utils.unmarshal_json", wraps=unmarshal_json) as reparse,
            self.assertRaises(errors.ResponseValidationError) as ctx,
        ):
            llm.chat([{"role": "user", "content": "hello"}])
        self.assertEqual(ctx.exception.raw_response.status_code, 400)
        reparse.assert_not_called()

    def test_content_parts_join_to_text(self):
        parts = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        self.responses.append(
            httpx.Response(200, json=completion({"role": "assistant", "content": parts}, None))
        )
        resp = llm.chat([{"role": "user", "content": "hello"}])
        self.assertEqual(resp.choices[0].message.content, "ab")

    def test_retry_after_is_not_honoured(self):
        self.responses += [
            httpx.Response(429, headers={"Retry-After": "3600"}, json={"error": {}}),
            httpx.Response(200, json=completion({"role": "assistant", "content": "hi"}, None)),
        ]
        with mock.patch("openrouter.utils.retries.time.sleep") as sleep:
            resp = llm.chat([{"role": "user", "content": "hello"}])
        self.assertEqual(resp.choices[0].message.content, "hi")
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(sleep.call_args_list)
        for call in sleep.call_args_list:
            self.assertLessEqual(call.args[0], 4.5)

    def test_debug_env_does_not_log_bodies(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_DEBUG": "1"}):
            client = llm._get_client()
        self.assertIs(type(client.sdk_configuration.debug_logger), NoOpLogger)


class FillOptionalFieldsTest(unittest.TestCase):
    def test_fills_only_missing_optional_fields(self):
        body = {"id": "gen-1", "choices": [{"message": {}}, {"index": 7, "finish_reason": "stop"}]}
        self.assertEqual(
            json.loads(llm._fill_optional_fields(json.dumps(body))),
            {
                "id": "gen-1",
                "system_fingerprint": None,
                "choices": [
                    {"message": {}, "finish_reason": None, "index": 0},
                    {"index": 7, "finish_reason": "stop"},
                ],
            },
        )

    def test_no_choices_key_is_left_absent(self):
        out = json.loads(llm._fill_optional_fields('{"system_fingerprint": "fp"}'))
        self.assertEqual(out, {"system_fingerprint": "fp"})


class RecordSignatureTest(unittest.TestCase):
    FIELDS = dict(
        purpose="chat",
        model="m",
        thread_id="t",
        estimated_tokens=1,
        prompt_tokens=1,
        completion_tokens=1,
        usage_missing=False,
        over_limit=False,
    )

    def test_extra_or_missing_field_is_a_type_error(self):
        # A typo'd or extra key must fail at the call site, not reach the JSONL log.
        with self.assertRaises(TypeError):
            llm._record(**self.FIELDS, message="body text")
        with self.assertRaises(TypeError):
            llm._record(**{k: v for k, v in self.FIELDS.items() if k != "over_limit"})


if __name__ == "__main__":
    unittest.main()
