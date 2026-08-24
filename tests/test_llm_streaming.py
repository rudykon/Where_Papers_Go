from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from where_paper_go.enrichment import (
    OpenAIStreamError,
    PageText,
    call_llm,
    chat_message_text,
    openai_chat_request,
    parse_openai_chat_stream,
)


def sse_event(payload: object) -> bytes:
    return (
        "data: " + json.dumps(payload, ensure_ascii=False) + "\r\n\r\n"
    ).encode("utf-8")


def streamed_scope(*, finish_reason: str = "stop", done: bool = True) -> bytes:
    result = json.dumps(
        {
            "is_relevant": True,
            "scope_summary": "医学影像与可信学习",
            "scope_keywords": ["医学影像", "可信学习"],
            "source_url": "https://publisher.example/scope",
            "evidence": "The journal covers medical imaging.",
            "confidence": "high",
        },
        ensure_ascii=False,
    )
    midpoint = len(result) // 2
    body = b": keepalive\r\n\r\n"
    body += sse_event(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "reasoning_content": "private reasoning",
                        "content": result[:midpoint],
                    },
                    "finish_reason": None,
                }
            ]
        }
    )
    body += sse_event(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "content": result[midpoint:],
                        "annotations": [{"type": "url_citation", "url": "https://publisher.example/scope"}],
                    },
                    "finish_reason": finish_reason,
                }
            ]
        }
    )
    body += sse_event({"choices": [], "usage": {"completion_tokens": 80}})
    if done:
        body += b"data: [DONE]\r\n\r\n"
    return body


class OpenAIStreamingTests(unittest.TestCase):
    def test_fragmented_sse_reassembles_content_reasoning_and_annotations(self) -> None:
        raw = streamed_scope()
        chunks = [raw[:17], raw[17:91], raw[91:173], raw[173:]]

        response, metadata = parse_openai_chat_stream(chunks)
        choice = response["choices"][0]
        message = choice["message"]

        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(json.loads(message["content"])["scope_summary"], "医学影像与可信学习")
        self.assertEqual(message["reasoning_content"], "private reasoning")
        self.assertEqual(message["annotations"][0]["type"], "url_citation")
        self.assertEqual(chat_message_text(message), message["content"])
        self.assertTrue(metadata["streamed"])
        self.assertTrue(metadata["stream_complete"])
        self.assertEqual(metadata["stream_events"], 3)

    def test_incomplete_sse_fails_closed_but_terminal_compatibility_is_configurable(self) -> None:
        raw = streamed_scope(done=False)
        with self.assertRaisesRegex(OpenAIStreamError, r"before \[DONE\]"):
            parse_openai_chat_stream([raw], require_done=True)

        response, metadata = parse_openai_chat_stream([raw], require_done=False)
        self.assertEqual(response["choices"][0]["finish_reason"], "stop")
        self.assertTrue(metadata["stream_complete"])

    def test_stream_requested_accepts_conventional_json_fallback(self) -> None:
        body = json.dumps(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        ).encode("utf-8")

        response, metadata = parse_openai_chat_stream([body])

        self.assertEqual(response["choices"][0]["message"]["content"], "ok")
        self.assertFalse(metadata["streamed"])

    def test_error_event_and_transport_limit_fail_closed(self) -> None:
        with self.assertRaisesRegex(OpenAIStreamError, "error event"):
            parse_openai_chat_stream(
                [sse_event({"error": {"message": "gateway failed"}}) + b"data: [DONE]\n\n"]
            )
        with self.assertRaisesRegex(OpenAIStreamError, "transport limit"):
            parse_openai_chat_stream([b"x" * 100], max_bytes=32)

    def test_model_specific_total_timeout_overrides_generic_stream_limit(self) -> None:
        config = {
            "api_key": "offline-key",
            "stream": True,
            "stream_idle_timeout": 60,
            "stream_total_timeout": 180,
            "model_stream_total_timeouts": {"Qwen3.6-35B": 90},
        }
        with patch(
            "where_paper_go.enrichment.http_stream_request",
            return_value=(200, {}, streamed_scope()),
        ) as request:
            _status, _headers, content = openai_chat_request(
                "https://llmapi.pcl.ac.cn/v1/chat/completions",
                payload={"model": "Qwen3.6-35B", "messages": []},
                config=config,
            )

        self.assertEqual(request.call_args.kwargs["total_timeout"], 90)
        self.assertEqual(
            json.loads(content)["choices"][0]["finish_reason"], "stop"
        )

    def test_scope_llm_streams_then_caches_only_complete_json(self) -> None:
        config = {
            "llm": {
                "api_key": "offline-key",
                "base_url": "https://llmapi.pcl.ac.cn/v1",
                "model": "DeepSeek-V4-Pro",
                "stream": True,
                "stream_require_done": True,
                "stream_idle_timeout": 60,
                "stream_total_timeout": 180,
                "max_retries": 0,
            }
        }
        row = {"name": "Test Journal", "record_type": "journal"}
        pages = [
            PageText(
                url="https://publisher.example/scope",
                title="Aims and scope",
                text="The journal covers medical imaging and trustworthy learning.",
                links=[],
            )
        ]
        captured_payloads: list[dict[str, object]] = []

        def fake_stream(url, **kwargs):
            del url
            captured_payloads.append(json.loads(bytes(kwargs["body"]).decode("utf-8")))
            return 200, {"content-type": "text/event-stream"}, streamed_scope()

        with tempfile.TemporaryDirectory() as temporary, patch(
            "where_paper_go.enrichment.http_stream_request",
            side_effect=fake_stream,
        ) as request:
            result = call_llm(row, pages, config, Path(temporary), 45, 8_000)
            cached = call_llm(row, pages, config, Path(temporary), 45, 8_000)

        self.assertEqual(result, cached)
        self.assertEqual(result["scope_summary"], "医学影像与可信学习")
        self.assertEqual(request.call_count, 1)
        self.assertIs(captured_payloads[0]["stream"], True)

    def test_scope_does_not_cache_an_incomplete_stream(self) -> None:
        config = {
            "llm": {
                "api_key": "offline-key",
                "base_url": "https://llmapi.pcl.ac.cn/v1",
                "model": "DeepSeek-V4-Pro",
                "stream": True,
                "stream_require_done": True,
                "max_retries": 0,
            }
        }
        pages = [PageText("https://publisher.example/scope", "Scope", "Evidence", [])]
        with tempfile.TemporaryDirectory() as temporary, patch(
            "where_paper_go.enrichment.http_stream_request",
            return_value=(200, {}, streamed_scope(done=False)),
        ):
            cache_root = Path(temporary)
            with self.assertRaises(OpenAIStreamError):
                call_llm({"name": "Test Journal"}, pages, config, cache_root, 45, 8_000)
            self.assertFalse(any((cache_root / "llm").glob("*.json")))


if __name__ == "__main__":
    unittest.main()
