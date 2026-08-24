from __future__ import annotations

from dataclasses import replace
import fcntl
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from where_paper_go.enrichment import PageText

from research.baselines import TfidfBaseline
from research.clean_corpus import rebuild_clean_corpus
from research.cli import _parser
from research.data import (
    DatasetBundle,
    ResearchDataError,
    load_jsonl_corpus,
    normalize_text,
)
from research.historical_builder import (
    CollectionPolicy,
    CrossrefHistoricalSource,
    HistoricalCollectionError,
    OfficialScopeSearchSource,
    PCLPrototypeClient,
    PCLRetryPolicy,
    VenueSeed,
    assemble_historical_corpus,
    build_venue_profile,
    catalog_evidence,
    merge_paper_evidence,
    paper_evidence_id,
    process_venue,
    run_historical_collection,
    stable_collection_queue,
)
from research.pcl_retry import PCLRetryOutcome, PCLRetryQueue
from research.prototype_vectors import build_prototype_vector_run
from research.types import Query, VenueDocument


def _venue(identifier: str = "jcr-test", name: str = "Test Journal") -> VenueSeed:
    return VenueSeed(
        venue_id=identifier,
        name=name,
        issns=("00079235",),
        quartile="Q1",
        subject="ONCOLOGY",
        subject_en="Oncology",
        broad_field="clinical_medicine",
    )


def _paper(
    source: str,
    evidence_id: str,
    *,
    doi: str = "",
    title: str = "A title-only cancer imaging study",
    abstract: str = "",
) -> dict[str, object]:
    return {
        "evidence_id": evidence_id,
        "venue_id": "jcr-test",
        "kind": "paper",
        "source": source,
        "doi": doi,
        "title": title,
        "abstract": abstract,
        "publication_date": "2025-06-15",
        "publication_date_precision": "day",
        "url": "https://doi.org/" + doi if doi else "",
        "keywords": [],
        "temporal_eligible": True,
    }


def _pcl_envelope(result: dict[str, object]) -> tuple[int, dict[str, str], bytes]:
    payload = {
        "choices": [
            {"message": {"content": json.dumps(result)}}
        ]
    }
    return 200, {}, json.dumps(payload).encode("utf-8")


def _pcl_stream(
    result: dict[str, object],
    *,
    finish_reason: str = "stop",
    done: bool = True,
) -> bytes:
    content = json.dumps(result, ensure_ascii=False)
    midpoint = len(content) // 2
    chunks = [content[:midpoint], content[midpoint:]]
    body = b""
    for index, value in enumerate(chunks):
        event = {
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": value},
                    "finish_reason": finish_reason if index == len(chunks) - 1 else None,
                }
            ]
        }
        body += (
            "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        ).encode("utf-8")
    if done:
        body += b"data: [DONE]\n\n"
    return body


def _grounded_response(evidence_id: str, *, label: str = "Cancer imaging") -> dict[str, object]:
    return {
        "prototypes": [
            {
                "label": label,
                "summary": "Imaging methods for cancer diagnosis",
                "keywords": ["cancer", "imaging"],
                "evidence_ids": [evidence_id],
                "confidence": "high",
            }
        ]
    }


class FakeSource:
    def __init__(self, name: str, rows: list[dict[str, object]]) -> None:
        self.name = name
        self.rows = rows

    def fetch(self, venue: VenueSeed, policy: CollectionPolicy) -> list[dict[str, object]]:
        del venue, policy
        return [dict(row) for row in self.rows]


class FakePCL:
    model = "fake-pcl"
    provider_identity = {
        "provider": "pcl_openai_compatible",
        "endpoint_host": "llmapi.pcl.ac.cn",
        "model": model,
    }

    def synthesize(self, venue, evidence, policy):
        del policy
        paper = next(row for row in evidence if row.get("kind") == "paper")
        evidence_id = str(paper["evidence_id"])
        return (
            [
                {
                    "prototype_id": venue.venue_id + ":pcl:0",
                    "kind": "historical_topic",
                    "label": "Cancer imaging",
                    "text": "cancer imaging diagnosis",
                    "keywords": ["cancer", "imaging"],
                    "weight": 1.0,
                    "confidence": "high",
                    "source_ids": [evidence_id],
                    "source_max_date": "2025-06-15",
                    "temporal_eligible": True,
                    "derived_by": "pcl_llm",
                    "model": self.model,
                    "generation": {
                        "model": self.model,
                        "prompt_version": "offline-test-v1",
                        "prompt_sha256": "a" * 64,
                        "parameters": {"temperature": 0},
                        "parameters_sha256": "b" * 64,
                        "input_evidence_sha256": "c" * 64,
                        "input_evidence_count": len(evidence),
                        "code_state": {"commit": "offline-test", "dirty": False},
                    },
                }
            ],
            "ok",
        )


class SequencedPCL(FakePCL):
    """Offline PCL double whose outcomes are deterministic per call."""

    def __init__(self, outcomes: list[BaseException | str]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def synthesize(self, venue, evidence, policy):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if isinstance(outcome, BaseException):
            raise outcome
        return super().synthesize(venue, evidence, policy)


class CountingSource(FakeSource):
    def __init__(self, name: str, rows: list[dict[str, object]]) -> None:
        super().__init__(name, rows)
        self.calls = 0

    def fetch(self, venue: VenueSeed, policy: CollectionPolicy) -> list[dict[str, object]]:
        self.calls += 1
        return super().fetch(venue, policy)


class FailingSource:
    def __init__(self, name: str, error: BaseException) -> None:
        self.name = name
        self.error = error
        self.calls = 0

    def fetch(self, venue: VenueSeed, policy: CollectionPolicy) -> list[dict[str, object]]:
        del venue, policy
        self.calls += 1
        raise self.error


class AlwaysFailPCL(FakePCL):
    def __init__(self) -> None:
        self.calls = 0

    def synthesize(self, venue, evidence, policy):
        del venue, evidence, policy
        self.calls += 1
        raise TimeoutError("offline persistent timeout")


class FakeEmbeddingProvider:
    model = "fake-bge-m3"
    fingerprint = "fake-pcl-bge-m3"
    batch_size = 8

    def prepare_text(self, text: str) -> str:
        return " ".join(text.lower().split())

    def embed(self, texts):
        keys = ("quantum", "cancer", "robot", "ethics", "graph", "history", "medical", "other")
        vectors = []
        for text in texts:
            normalized = self.prepare_text(text)
            vector = [float(normalized.count(key)) for key in keys]
            if not any(vector):
                vector[-1] = 1.0
            vectors.append(vector)
        return vectors


class HistoricalCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = CollectionPolicy(
            history_start="2021-01-01",
            cutoff="2026-03-31",
            max_papers_per_venue=20,
            min_papers_before_fallback=2,
            openalex_mode="always",
            scope_mode="off",
            max_prototypes=6,
        )

    def test_collection_policy_rejects_nonpositive_pcl_evidence_limit(self) -> None:
        for value in (0, -1):
            with self.subTest(max_pcl_evidence=value), self.assertRaises(
                ResearchDataError
            ):
                replace(self.policy, max_pcl_evidence=value).validate()

    def test_crossref_history_accepts_title_only_and_omits_abstract_filter(self) -> None:
        class Client:
            def __init__(self):
                self.urls = []

            def get(self, source, url):
                self.urls.append((source, url))
                return {
                    "message": {
                        "items": [
                            {
                                "DOI": "10.1/title-only",
                                "title": ["Useful title without an abstract"],
                                "type": "journal-article",
                                "ISSN": ["0007-9235"],
                                "published-online": {"date-parts": [[2025, 5, 2]]},
                            }
                        ]
                    }
                }

        client = Client()
        rows = CrossrefHistoricalSource(client).fetch(_venue(), self.policy)  # type: ignore[arg-type]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["abstract"], "")
        self.assertNotIn("has-abstract", client.urls[0][1])

    def test_historical_cli_exposes_independent_pcl_retry_controls(self) -> None:
        defaults = _parser().parse_args(
            [
                "collect-historical-corpus",
                "--api-config",
                "api.json",
                "--jcr-csv",
                "jcr.csv",
                "--data-dir",
                "data",
                "--output-dir",
                "output",
                "--history-start",
                "2021-01-01",
                "--cutoff",
                "2026-03-31",
            ]
        )
        self.assertEqual(defaults.workers, 6)
        self.assertEqual(defaults.pcl_workers, 3)
        self.assertEqual(defaults.scope_workers, 1)

        args = _parser().parse_args(
            [
                "collect-historical-corpus",
                "--api-config",
                "api.json",
                "--jcr-csv",
                "jcr.csv",
                "--data-dir",
                "data",
                "--output-dir",
                "output",
                "--history-start",
                "2021-01-01",
                "--cutoff",
                "2026-03-31",
                "--pcl-retries",
                "4",
                "--pcl-backoff-base",
                "0.75",
                "--pcl-max-tokens",
                "640",
                "--pcl-workers",
                "2",
                "--pcl-models",
                "Model-A",
                "Model-B",
                "--pcl-model-fallbacks",
                "1",
            ]
        )
        self.assertEqual(args.pcl_retries, 4)
        self.assertEqual(args.pcl_backoff_base, 0.75)
        self.assertEqual(args.pcl_max_tokens, 640)
        self.assertEqual(args.pcl_workers, 2)
        self.assertEqual(args.pcl_models, ["Model-A", "Model-B"])
        self.assertEqual(args.pcl_model_fallbacks, 1)

    def test_collection_output_directory_rejects_a_second_process_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "out"
            output.mkdir()
            lock_path = output / ".collector.lock"
            with lock_path.open("a+", encoding="utf-8") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_handle.write('{"pid":999,"started_at":"offline-test"}\n')
                lock_handle.flush()
                with self.assertRaisesRegex(
                    HistoricalCollectionError,
                    "already holds",
                ):
                    run_historical_collection(
                        venues=[_venue()],
                        policy=self.policy,
                        output_dir=output,
                        jcr_csv=root / "unused.csv",
                        crossref=None,
                        openalex=None,
                        scope_search=None,
                        pcl=FakePCL(),
                        dry_run=True,
                    )
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def test_multi_source_doi_merge_keeps_longer_abstract_and_all_sources(self) -> None:
        rows = merge_paper_evidence(
            [
                _paper("crossref", "crossref:1", doi="10.1/shared"),
                _paper(
                    "openalex",
                    "openalex:1",
                    doi="https://doi.org/10.1/shared",
                    abstract="Detailed abstract supplied by a second source.",
                ),
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["abstract"], "Detailed abstract supplied by a second source.")
        self.assertEqual(rows[0]["sources"], ["crossref", "openalex"])

    def test_unicode_title_identities_are_preserved_and_venue_aware(self) -> None:
        titles = (
            "한국어 암 영상 진단",
            "Русское исследование рака",
            "پژوهش تشخیص سرطان",
            "日本語のがん画像診断",
        )
        normalized = [normalize_text(title) for title in titles]
        self.assertTrue(all(normalized))
        self.assertEqual(len(set(normalized)), len(titles))
        identities = {
            paper_evidence_id(
                "jcr-one", title=title, published="2025-06-15"
            )
            for title in titles
        }
        self.assertEqual(len(identities), len(titles))
        self.assertNotEqual(
            paper_evidence_id(
                "jcr-one", title=titles[0], published="2025-06-15"
            ),
            paper_evidence_id(
                "jcr-two", title=titles[0], published="2025-06-15"
            ),
        )

    def test_process_venue_builds_grounded_multi_prototype_profile(self) -> None:
        crossref = FakeSource(
            "crossref",
            [
                _paper("crossref", "crossref:title-only"),
                _paper(
                    "crossref",
                    "crossref:abstract",
                    title="A second paper",
                    abstract="Explicit cancer diagnosis abstract.",
                ),
            ],
        )
        result = process_venue(
            _venue(),
            policy=self.policy,
            crossref=crossref,
            openalex=FakeSource("openalex", []),
            scope_search=None,
            pcl=FakePCL(),
        )
        profile = result["profile"]
        metadata = profile["metadata"]
        self.assertEqual(metadata["history_paper_count"], 2)
        self.assertEqual(metadata["title_only_paper_count"], 1)
        self.assertEqual(metadata["abstract_paper_count"], 1)
        self.assertEqual(metadata["profile_tier"], "few-shot")
        self.assertGreaterEqual(len(profile["prototypes"]), 2)
        self.assertEqual(metadata["pcl_status"], "ok")

    def test_pcl_request_applies_output_limit(self) -> None:
        """Prototype generation must put an explicit upper bound on model output."""

        config = {
            "llm": {
                "api_key": "offline-test-key",
                "base_url": "https://llmapi.pcl.ac.cn/v1",
                "model": "fake-pcl",
                "timeout": 9,
            }
        }
        response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "prototypes": [
                                    {
                                        "label": "Cancer imaging",
                                        "summary": "Imaging methods for cancer diagnosis",
                                        "keywords": ["cancer", "imaging"],
                                        "evidence_ids": ["crossref:1"],
                                        "confidence": "high",
                                    }
                                ]
                            }
                        )
                    }
                }
            ]
        }

        def fake_request(url, **kwargs):
            del url
            return 200, {}, json.dumps(response).encode("utf-8")

        with tempfile.TemporaryDirectory() as temporary, patch(
            "research.historical_builder.enrichment.http_request",
            side_effect=fake_request,
        ) as request:
            client = PCLPrototypeClient(
                config,
                Path(temporary),
                max_output_tokens=321,
            )
            prototypes, status = client.synthesize(
                _venue(),
                [_paper("crossref", "crossref:1")],
                self.policy,
            )

        self.assertEqual(status, "ok")
        self.assertEqual(len(prototypes), 1)
        self.assertEqual(request.call_count, 1)
        payload = json.loads(bytes(request.call_args.kwargs["body"]).decode("utf-8"))
        self.assertEqual(payload["max_tokens"], 321)

    def test_pcl_prompt_excludes_every_non_temporal_evidence_row(self) -> None:
        config = {
            "llm": {
                "api_key": "offline-test-key",
                "base_url": "https://llmapi.pcl.ac.cn/v1",
                "model": "fake-pcl",
            }
        }
        captured_prompt = ""

        def fake_request(url, **kwargs):
            nonlocal captured_prompt
            del url
            payload = json.loads(bytes(kwargs["body"]).decode("utf-8"))
            captured_prompt = json.dumps(payload["messages"], ensure_ascii=False)
            return _pcl_envelope(_grounded_response("crossref:temporal"))

        future = {
            "evidence_id": "official:future",
            "venue_id": "jcr-test",
            "kind": "official_scope",
            "source": "search:tavily",
            "text": "future-only prompt contamination",
            "valid_at": "2026-08-14",
            "temporal_eligible": False,
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "research.historical_builder.enrichment.http_request",
            side_effect=fake_request,
        ):
            client = PCLPrototypeClient(config, Path(temporary))
            prototypes, status = client.synthesize(
                _venue(),
                [_paper("crossref", "crossref:temporal"), future],
                self.policy,
            )

        self.assertEqual(status, "ok")
        self.assertNotIn("future-only", captured_prompt)
        self.assertNotIn("official:future", captured_prompt)
        generation = prototypes[0]["generation"]
        self.assertEqual(generation["prompt_version"], client.PROMPT_VERSION)
        self.assertEqual(generation["input_evidence_count"], 1)
        self.assertTrue(generation["prompt_sha256"])

    def test_pcl_streams_and_caches_only_the_complete_grounded_result(self) -> None:
        config = {
            "llm": {
                "api_key": "offline-test-key",
                "base_url": "https://llmapi.pcl.ac.cn/v1",
                "model": "Qwen3.6-35B",
                "stream": True,
                "stream_require_done": True,
                "stream_idle_timeout": 60,
                "stream_total_timeout": 180,
            }
        }
        payloads: list[dict[str, object]] = []

        def fake_stream(url, **kwargs):
            del url
            payloads.append(json.loads(bytes(kwargs["body"]).decode("utf-8")))
            return (
                200,
                {"content-type": "text/event-stream"},
                _pcl_stream(_grounded_response("crossref:1")),
            )

        with tempfile.TemporaryDirectory() as temporary, patch(
            "research.historical_builder.enrichment.http_stream_request",
            side_effect=fake_stream,
        ) as request:
            root = Path(temporary)
            client = PCLPrototypeClient(config, root, max_output_tokens=8192)
            prototypes, status = client.synthesize(
                _venue(), [_paper("crossref", "crossref:1")], self.policy
            )
            cached, cached_status = client.synthesize(
                _venue(), [_paper("crossref", "crossref:1")], self.policy
            )
            audit = [
                json.loads(line)
                for line in (root / "pcl_model_attempts.jsonl").read_text().splitlines()
            ]

        self.assertEqual(status, "ok")
        self.assertEqual(cached_status, "ok")
        self.assertEqual(prototypes, cached)
        self.assertEqual(request.call_count, 1)
        self.assertIs(payloads[0]["stream"], True)
        self.assertTrue(audit[0]["streamed"])
        self.assertGreaterEqual(audit[0]["stream_events"], 2)

    def test_pcl_length_stream_never_caches_parseable_partial_result(self) -> None:
        config = {
            "llm": {
                "api_key": "offline-test-key",
                "base_url": "https://llmapi.pcl.ac.cn/v1",
                "model": "Model-A",
                "models": ["Model-A", "Model-B"],
                "stream": True,
                "stream_require_done": True,
            }
        }
        payloads: list[dict[str, object]] = []

        def fake_stream(url, **kwargs):
            del url
            payload = json.loads(bytes(kwargs["body"]).decode("utf-8"))
            payloads.append(payload)
            finish_reason = "length" if payload["model"] == "Model-A" else "stop"
            return (
                200,
                {},
                _pcl_stream(
                    _grounded_response("crossref:1"),
                    finish_reason=finish_reason,
                ),
            )

        with tempfile.TemporaryDirectory() as temporary, patch(
            "research.historical_builder.enrichment.http_stream_request",
            side_effect=fake_stream,
        ):
            root = Path(temporary)
            client = PCLPrototypeClient(config, root, model_fallbacks=1)
            prototypes, status = client.synthesize(
                _venue(), [_paper("crossref", "crossref:1")], self.policy
            )
            cache_files = list((root / "pcl_prototypes").glob("*.json"))
            audit = [
                json.loads(line)
                for line in (root / "pcl_model_attempts.jsonl").read_text().splitlines()
            ]

        self.assertEqual(status, "ok")
        self.assertEqual(prototypes[0]["model"], "Model-B")
        self.assertEqual([row["model"] for row in audit], ["Model-A", "Model-B"])
        self.assertEqual(audit[0]["status"], "truncated_response")
        self.assertEqual(len(cache_files), 1)
        self.assertEqual(payloads[0]["max_tokens"], 2048)
        self.assertEqual(payloads[1]["max_tokens"], 3072)

    def test_pcl_model_pool_switches_after_invalid_response_and_sends_actual_model(self) -> None:
        config = {
            "llm": {
                "api_key": "offline-test-key",
                "base_url": "https://llmapi.pcl.ac.cn/v1",
                "model": "Model-A",
                "models": ["Model-A", "Model-B"],
                "model_max_output_tokens": {"Model-B": 4096},
            }
        }
        responses = [
            _pcl_envelope({"not_prototypes": []}),
            _pcl_envelope(_grounded_response("crossref:1")),
            _pcl_envelope(_grounded_response("crossref:1", label="Second venue")),
        ]
        requested_models: list[str] = []
        requested_tokens: list[int] = []

        def fake_request(url, **kwargs):
            del url
            payload = json.loads(bytes(kwargs["body"]).decode("utf-8"))
            requested_models.append(payload["model"])
            requested_tokens.append(payload["max_tokens"])
            return responses.pop(0)

        with tempfile.TemporaryDirectory() as temporary, patch(
            "research.historical_builder.enrichment.http_request",
            side_effect=fake_request,
        ):
            client = PCLPrototypeClient(
                config,
                Path(temporary),
                model_fallbacks=1,
            )
            prototypes, status = client.synthesize(
                _venue(),
                [_paper("crossref", "crossref:1")],
                self.policy,
            )
            second_prototypes, second_status = client.synthesize(
                _venue("jcr-second", "Second Journal"),
                [_paper("crossref", "crossref:1")],
                self.policy,
            )
            hard_limited = PCLPrototypeClient(
                config,
                Path(temporary) / "hard-limited",
                max_output_tokens=2048,
                model_fallbacks=1,
            )

        self.assertEqual(status, "ok")
        self.assertEqual(second_status, "ok")
        self.assertEqual(requested_models, ["Model-A", "Model-B", "Model-B"])
        self.assertEqual(requested_tokens, [2048, 4096, 4096])
        self.assertEqual(prototypes[0]["model"], "Model-B")
        self.assertEqual(second_prototypes[0]["model"], "Model-B")
        self.assertEqual(hard_limited.model_output_tokens["Model-B"], 2048)

    def test_pcl_models_selects_active_pool_before_full_model_catalog(self) -> None:
        config = {
            "llm": {
                "api_key": "offline-test-key",
                "base_url": "https://llmapi.pcl.ac.cn/v1",
                "model": "Scope-Model",
                "models": ["Unused-A", "Unused-B"],
                "pcl_models": ["Model-A", "Model-B"],
            }
        }

        with tempfile.TemporaryDirectory() as temporary:
            client = PCLPrototypeClient(config, Path(temporary), model_fallbacks=1)

        self.assertEqual(client.models, ("Model-A", "Model-B"))
        self.assertEqual(client.provider_identity["model"], "Model-A")

    def test_pcl_cache_is_isolated_by_actual_model(self) -> None:
        requested_models: list[str] = []

        def config(model: str) -> dict[str, object]:
            return {
                "llm": {
                    "api_key": "offline-test-key",
                    "base_url": "https://llmapi.pcl.ac.cn/v1",
                    "model": model,
                    "models": [model],
                }
            }

        def fake_request(url, **kwargs):
            del url
            payload = json.loads(bytes(kwargs["body"]).decode("utf-8"))
            model = str(payload["model"])
            requested_models.append(model)
            return _pcl_envelope(
                _grounded_response("crossref:1", label=f"Result from {model}")
            )

        with tempfile.TemporaryDirectory() as temporary, patch(
            "research.historical_builder.enrichment.http_request",
            side_effect=fake_request,
        ):
            cache_dir = Path(temporary)
            first, first_status = PCLPrototypeClient(
                config("Model-A"), cache_dir
            ).synthesize(
                _venue(),
                [_paper("crossref", "crossref:1")],
                self.policy,
            )
            second, second_status = PCLPrototypeClient(
                config("Model-B"), cache_dir
            ).synthesize(
                _venue(),
                [_paper("crossref", "crossref:1")],
                self.policy,
            )
            cached, cached_status = PCLPrototypeClient(
                config("Model-A"), cache_dir
            ).synthesize(
                _venue(),
                [_paper("crossref", "crossref:1")],
                self.policy,
            )

        self.assertEqual(requested_models, ["Model-A", "Model-B"])
        self.assertEqual((first_status, second_status, cached_status), ("ok", "ok", "ok"))
        self.assertEqual(first[0]["model"], "Model-A")
        self.assertEqual(second[0]["model"], "Model-B")
        self.assertEqual(cached[0]["model"], "Model-A")

    def test_pcl_context_budget_and_unknown_minimax_cap_are_conservative(self) -> None:
        known_config = {
            "llm": {
                "api_key": "offline-test-key",
                "base_url": "https://llmapi.pcl.ac.cn/v1",
                "model": "Qwen3.6-35B",
                "models": ["Qwen3.6-35B"],
                "model_context_windows": {"Qwen3.6-35B": 16_384},
            }
        }
        captured_payloads: list[dict[str, object]] = []

        def fake_request(url, **kwargs):
            del url
            payload = json.loads(bytes(kwargs["body"]).decode("utf-8"))
            captured_payloads.append(payload)
            prompt = json.dumps(payload["messages"], ensure_ascii=False)
            evidence_id = prompt.split("[Evidence ", 1)[1].split("]", 1)[0]
            return _pcl_envelope(_grounded_response(evidence_id))

        evidence = [
            _paper(
                "crossref",
                f"crossref:{index:02d}",
                abstract=("large evidence payload " * 80),
            )
            for index in range(20)
        ]
        policy = replace(self.policy, max_pcl_evidence=20)
        with tempfile.TemporaryDirectory() as temporary, patch(
            "research.historical_builder.enrichment.http_request",
            side_effect=fake_request,
        ):
            client = PCLPrototypeClient(
                known_config,
                Path(temporary),
                max_output_tokens=512,
            )
            _prototypes, status = client.synthesize(_venue(), evidence, policy)
            minimax = PCLPrototypeClient(
                {
                    "llm": {
                        "api_key": "offline-test-key",
                        "base_url": "https://llmapi.pcl.ac.cn/v1",
                        "model": "MiniMax-M3",
                        "models": ["MiniMax-M3"],
                    }
                },
                Path(temporary) / "minimax",
            )

        self.assertEqual(status, "ok")
        self.assertEqual(client.context_windows["Qwen3.6-35B"], 16_384)
        self.assertEqual(client.model_input_caps["Qwen3.6-35B"], 7_680)
        prompt_bytes = len(
            json.dumps(
                captured_payloads[0]["messages"],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        self.assertLessEqual(prompt_bytes, 7_680)
        self.assertLess(
            json.dumps(captured_payloads[0]["messages"]).count("[Evidence "),
            len(evidence),
        )
        self.assertIsNone(minimax.context_windows.get("MiniMax-M3"))
        self.assertEqual(minimax.model_input_caps["MiniMax-M3"], 32_768)

    def test_pcl_model_pool_propagates_transport_failure_but_keeps_semantic_status(self) -> None:
        config = {
            "llm": {
                "api_key": "offline-test-key",
                "base_url": "https://llmapi.pcl.ac.cn/v1",
                "model": "Model-A",
                "models": ["Model-A", "Model-B"],
            }
        }
        evidence = [_paper("crossref", "crossref:1")]

        with self.subTest("all transport failures"), tempfile.TemporaryDirectory() as temporary:
            requested_models: list[str] = []
            errors: list[BaseException] = [
                TimeoutError("first transport failure"),
                ConnectionError("last transport failure"),
            ]

            def fail_transport(url, **kwargs):
                del url
                payload = json.loads(bytes(kwargs["body"]).decode("utf-8"))
                requested_models.append(payload["model"])
                raise errors.pop(0)

            with patch(
                "research.historical_builder.enrichment.http_request",
                side_effect=fail_transport,
            ):
                client = PCLPrototypeClient(
                    config,
                    Path(temporary),
                    model_fallbacks=1,
                )
                with self.assertRaisesRegex(ConnectionError, "last transport failure"):
                    client.synthesize(_venue(), evidence, self.policy)
            self.assertEqual(requested_models, ["Model-A", "Model-B"])

        with self.subTest("semantic failure wins"), tempfile.TemporaryDirectory() as temporary:
            responses: list[tuple[int, dict[str, str], bytes] | BaseException] = [
                _pcl_envelope({"not_prototypes": []}),
                TimeoutError("fallback transport failure"),
            ]

            def semantic_then_transport(url, **kwargs):
                del url, kwargs
                response = responses.pop(0)
                if isinstance(response, BaseException):
                    raise response
                return response

            with patch(
                "research.historical_builder.enrichment.http_request",
                side_effect=semantic_then_transport,
            ):
                client = PCLPrototypeClient(
                    config,
                    Path(temporary),
                    model_fallbacks=1,
                )
                prototypes, status = client.synthesize(
                    _venue(), evidence, self.policy
                )
            self.assertEqual(prototypes, [])
            self.assertEqual(status, "invalid_response")
            self.assertEqual(client.last_model, "Model-A")

    def test_pcl_does_not_retry_non_transient_http_error(self) -> None:
        """Authentication and other permanent HTTP errors should fail fast."""

        import urllib.error

        config = {
            "llm": {
                "api_key": "offline-test-key",
                "base_url": "https://llmapi.pcl.ac.cn/v1",
                "model": "fake-pcl",
            }
        }
        error = urllib.error.HTTPError(
            "https://llmapi.pcl.ac.cn/v1/chat/completions",
            401,
            "unauthorized",
            {},
            io.BytesIO(b""),
        )
        with tempfile.TemporaryDirectory() as temporary, patch(
            "research.historical_builder.enrichment.http_request",
            side_effect=error,
        ) as request, patch("research.historical_builder.time.sleep") as sleep:
            client = PCLPrototypeClient(
                config,
                Path(temporary),
                max_output_tokens=128,
            )
            with self.assertRaises(urllib.error.HTTPError):
                client.synthesize(
                    _venue(),
                    [_paper("crossref", "crossref:1")],
                    self.policy,
                )

        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()
        error.close()

    def test_runner_retries_transient_pcl_failures_with_exponential_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jcr = root / "jcr.csv"
            jcr.write_text("name\nfixture\n", encoding="utf-8")
            crossref = CountingSource(
                "crossref",
                [_paper("crossref", "crossref:1", abstract="cancer imaging")],
            )
            pcl = SequencedPCL(
                [
                    TimeoutError("offline timeout one"),
                    TimeoutError("offline timeout two"),
                    "ok",
                ]
            )
            run_historical_collection(
                venues=[_venue()],
                policy=self.policy,
                output_dir=root / "out",
                jcr_csv=jcr,
                crossref=crossref,
                openalex=None,
                scope_search=None,
                pcl=pcl,
                batch_size=50,
                workers=1,
                smoke_limit=1,
                pcl_retry_policy=PCLRetryPolicy(
                    max_attempts=3,
                    second_pass_attempts=1,
                    backoff_base=0.25,
                    backoff_max=10.0,
                    workers=1,
                ),
            )
            queue_events = [
                json.loads(line)
                for line in (root / "out" / "pcl_retry_queue.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]

        self.assertEqual(crossref.calls, 1)
        self.assertEqual(pcl.calls, 3)
        self.assertEqual(
            [
                row["delay_seconds"]
                for row in queue_events
                if row.get("event") == "retrying"
            ],
            [0.25, 0.5],
        )

    def test_invalid_or_ungrounded_pcl_response_does_not_poison_cache(self) -> None:
        """A later retry must reach PCL instead of replaying an unusable cache entry."""

        config = {
            "llm": {
                "api_key": "offline-test-key",
                "base_url": "https://llmapi.pcl.ac.cn/v1",
                "model": "fake-pcl",
            }
        }
        valid = {
            "prototypes": [
                {
                    "label": "Cancer imaging",
                    "summary": "Imaging methods for cancer diagnosis",
                    "keywords": ["cancer", "imaging"],
                    "evidence_ids": ["crossref:1"],
                    "confidence": "high",
                }
            ]
        }
        unusable_responses = (
            ({"not_prototypes": []}, "invalid_response"),
            (
                {
                    "prototypes": [
                        {
                            "label": "Unsupported topic",
                            "summary": "No valid grounding",
                            "keywords": ["unsupported"],
                            "evidence_ids": ["not-an-allowed-id"],
                        }
                    ]
                },
                "ungrounded_response",
            ),
        )

        for unusable, expected_status in unusable_responses:
            with self.subTest(expected_status=expected_status):
                with tempfile.TemporaryDirectory() as temporary:
                    responses = [unusable, valid]

                    def fake_request(url, **kwargs):
                        del url, kwargs
                        result = responses.pop(0)
                        envelope = {
                            "choices": [
                                {"message": {"content": json.dumps(result)}}
                            ]
                        }
                        return 200, {}, json.dumps(envelope).encode("utf-8")

                    with patch(
                        "research.historical_builder.enrichment.http_request",
                        side_effect=fake_request,
                    ) as request:
                        client = PCLPrototypeClient(
                            config,
                            Path(temporary),
                            max_output_tokens=256,
                        )
                        first_prototypes, first_status = client.synthesize(
                            _venue(),
                            [_paper("crossref", "crossref:1")],
                            self.policy,
                        )
                        second_prototypes, second_status = client.synthesize(
                            _venue(),
                            [_paper("crossref", "crossref:1")],
                            self.policy,
                        )

                    self.assertEqual(first_prototypes, [])
                    self.assertEqual(first_status, expected_status)
                    self.assertEqual(second_status, "ok")
                    self.assertEqual(len(second_prototypes), 1)
                    self.assertEqual(request.call_count, 2)

    def test_failed_pcl_is_queued_and_retried_without_refetching_evidence(self) -> None:
        """The second PCL pass reuses the shard and preserves non-PCL errors."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jcr = root / "jcr.csv"
            jcr.write_text("name\nfixture\n", encoding="utf-8")
            venue = _venue()
            crossref = CountingSource(
                "crossref",
                [_paper("crossref", "crossref:1", abstract="cancer imaging")],
            )
            openalex = FailingSource("openalex", RuntimeError("offline source failure"))
            pcl = SequencedPCL([TimeoutError("offline timeout"), "ok"])

            run_historical_collection(
                venues=[venue],
                policy=self.policy,
                output_dir=root / "out",
                jcr_csv=jcr,
                crossref=crossref,
                openalex=openalex,
                scope_search=None,
                pcl=pcl,
                batch_size=50,
                workers=1,
                smoke_limit=1,
                pcl_retry_policy=PCLRetryPolicy(
                    max_attempts=1,
                    second_pass_attempts=1,
                    backoff_base=0,
                    workers=1,
                ),
            )

            shard = json.loads(
                (root / "out" / "venues" / "jcr-test.json").read_text(encoding="utf-8")
            )
            queue_path = root / "out" / "pcl_retry_queue.jsonl"
            queue_events = [
                json.loads(line)
                for line in queue_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            retry_attempts = [
                json.loads(line)
                for line in (root / "out" / "pcl_retry_attempts.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            retry_state = json.loads(
                (root / "out" / "pcl_retry_state.json").read_text(encoding="utf-8")
            )

        self.assertEqual(crossref.calls, 1)
        self.assertEqual(openalex.calls, 1)
        self.assertEqual(pcl.calls, 2)
        self.assertEqual(shard["pcl_status"], "ok")
        self.assertEqual(shard["status"], "partial")
        self.assertNotIn("pcl", shard["source_errors"])
        self.assertIn("openalex", shard["source_errors"])
        self.assertEqual(shard["profile"]["metadata"]["pcl_status"], "ok")
        self.assertTrue(
            any(row.get("derived_by") == "pcl_llm" for row in shard["profile"]["prototypes"])
        )
        self.assertGreaterEqual(len(queue_events), 2)
        self.assertEqual({row["venue_id"] for row in queue_events}, {"jcr-test"})
        self.assertTrue(any(row.get("status") == "queued" for row in queue_events))
        self.assertTrue(
            any(row.get("status") in {"ok", "completed"} for row in queue_events)
        )
        self.assertTrue(all("attempt" in row for row in queue_events))
        self.assertTrue(retry_attempts)
        self.assertEqual(
            {row["venue_id"] for row in retry_attempts},
            {"jcr-test"},
        )
        self.assertIsInstance(retry_state, dict)

    def test_pcl_retry_queue_resumes_after_restart_without_retry_partial(self) -> None:
        """A restart must discover a partial shard even if the event log is damaged."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "out"
            jcr = root / "jcr.csv"
            jcr.write_text("name\nfixture\n", encoding="utf-8")
            venue = _venue()
            first_source = CountingSource(
                "crossref",
                [_paper("crossref", "crossref:1", abstract="cancer imaging")],
            )
            failing_pcl = AlwaysFailPCL()
            run_historical_collection(
                venues=[venue],
                policy=self.policy,
                output_dir=output,
                jcr_csv=jcr,
                crossref=first_source,
                openalex=None,
                scope_search=None,
                pcl=failing_pcl,
                batch_size=50,
                workers=1,
                smoke_limit=1,
                pcl_retry_policy=PCLRetryPolicy(
                    max_attempts=1,
                    second_pass_attempts=1,
                    backoff_base=0,
                    workers=1,
                ),
            )
            partial = json.loads(
                (output / "venues" / "jcr-test.json").read_text(encoding="utf-8")
            )
            self.assertNotEqual(partial["pcl_status"], "ok")
            self.assertEqual(first_source.calls, 1)

            # Recovery must use the shard as the source of truth, not depend on
            # successfully parsing the append-only diagnostic event log.
            (output / "pcl_retry_queue.jsonl").write_text(
                "{truncated-after-crash\n", encoding="utf-8"
            )
            second_source = CountingSource("crossref", [])
            recovered_pcl = SequencedPCL(["ok"])
            run_historical_collection(
                venues=[venue],
                policy=self.policy,
                output_dir=output,
                jcr_csv=jcr,
                crossref=second_source,
                openalex=None,
                scope_search=None,
                pcl=recovered_pcl,
                batch_size=50,
                workers=1,
                smoke_limit=1,
                retry_partial=False,
                pcl_retry_policy=PCLRetryPolicy(
                    max_attempts=1,
                    second_pass_attempts=1,
                    backoff_base=0,
                    workers=1,
                ),
            )
            recovered = json.loads(
                (output / "venues" / "jcr-test.json").read_text(encoding="utf-8")
            )

        self.assertEqual(second_source.calls, 0)
        self.assertEqual(recovered_pcl.calls, 1)
        self.assertEqual(recovered["pcl_status"], "ok")
        self.assertEqual(recovered["status"], "complete")
        self.assertNotIn("pcl", recovered["source_errors"])

    def test_smoke_limit_bounds_historical_pcl_recovery_scan(self) -> None:
        """A one-venue smoke run must not enqueue every old PCL failure."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "out"
            shard_dir = output / "venues"
            shard_dir.mkdir(parents=True)
            jcr = root / "jcr.csv"
            jcr.write_text("name\nfixture\n", encoding="utf-8")
            venues = [
                _venue(f"jcr-old-{index}", f"Old failure {index}")
                for index in range(3)
            ]
            for index, venue in enumerate(venues):
                paper = _paper(
                    "crossref",
                    f"crossref:{index}",
                    abstract="cancer imaging",
                )
                paper["venue_id"] = venue.venue_id
                shard = process_venue(
                    venue,
                    policy=self.policy,
                    crossref=FakeSource("crossref", [paper]),
                    openalex=None,
                    scope_search=None,
                    pcl=AlwaysFailPCL(),
                )
                (shard_dir / f"{venue.venue_id}.json").write_text(
                    json.dumps(shard), encoding="utf-8"
                )

            source = CountingSource("crossref", [])
            pcl = SequencedPCL(["ok"])
            run_historical_collection(
                venues=venues,
                policy=self.policy,
                output_dir=output,
                jcr_csv=jcr,
                crossref=source,
                openalex=None,
                scope_search=None,
                pcl=pcl,
                batch_size=50,
                workers=1,
                smoke_limit=1,
                pcl_retry_policy=PCLRetryPolicy(
                    max_attempts=1,
                    second_pass_attempts=1,
                    backoff_base=0,
                    workers=1,
                ),
            )
            shards = [
                json.loads((shard_dir / f"{venue.venue_id}.json").read_text(encoding="utf-8"))
                for venue in venues
            ]

        self.assertEqual(source.calls, 0)
        self.assertEqual(pcl.calls, 1)
        self.assertEqual(sum(row["pcl_status"] == "ok" for row in shards), 1)
        self.assertEqual(
            sum(str(row["pcl_status"]).startswith("error:") for row in shards),
            2,
        )

    def test_smoke_limit_is_one_total_action_across_missing_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "out"
            shard_dir = output / "venues"
            shard_dir.mkdir(parents=True)
            jcr = root / "jcr.csv"
            jcr.write_text("name\nfixture\n", encoding="utf-8")
            missing = _venue("jcr-missing", "Missing venue")
            historical = [
                _venue(f"jcr-history-{index}", f"Historical failure {index}")
                for index in range(2)
            ]
            for index, venue in enumerate(historical):
                paper = _paper(
                    "crossref",
                    f"crossref:history:{index}",
                    abstract="cancer imaging",
                )
                paper["venue_id"] = venue.venue_id
                shard = process_venue(
                    venue,
                    policy=self.policy,
                    crossref=FakeSource("crossref", [paper]),
                    openalex=None,
                    scope_search=None,
                    pcl=AlwaysFailPCL(),
                )
                (shard_dir / f"{venue.venue_id}.json").write_text(
                    json.dumps(shard), encoding="utf-8"
                )

            source = CountingSource(
                "crossref",
                [_paper("crossref", "crossref:new", abstract="cancer imaging")],
            )
            pcl = SequencedPCL(["ok"])
            run_historical_collection(
                venues=[missing, *historical],
                policy=self.policy,
                output_dir=output,
                jcr_csv=jcr,
                crossref=source,
                openalex=None,
                scope_search=None,
                pcl=pcl,
                batch_size=50,
                workers=1,
                smoke_limit=1,
                pcl_retry_policy=PCLRetryPolicy(
                    max_attempts=1,
                    second_pass_attempts=1,
                    backoff_base=0,
                    workers=1,
                ),
            )
            missing_shard = json.loads(
                (shard_dir / "jcr-missing.json").read_text(encoding="utf-8")
            )
            historical_shards = [
                json.loads(
                    (shard_dir / f"{venue.venue_id}.json").read_text(encoding="utf-8")
                )
                for venue in historical
            ]

        self.assertEqual(source.calls, 1)
        self.assertEqual(pcl.calls, 1)
        self.assertEqual(missing_shard["pcl_status"], "ok")
        self.assertTrue(
            all(
                str(shard["pcl_status"]).startswith("error:")
                for shard in historical_shards
            )
        )

    def test_post_cutoff_scope_is_retained_but_excluded_from_frozen_profile(self) -> None:
        venue = _venue()
        evidence = catalog_evidence(venue, self.policy.cutoff)
        evidence.append(
            {
                "evidence_id": "official:current",
                "kind": "official_scope",
                "source": "search:tavily",
                "text": "future-only scope words",
                "valid_at": "2026-08-14",
                "temporal_eligible": False,
            }
        )
        profile = build_venue_profile(
            venue,
            evidence,
            (),
            cutoff=self.policy.cutoff,
            pcl_status="ok",
            pcl_model="fake",
            max_prototypes=6,
            collection_status="complete",
        )
        self.assertNotIn("future-only", profile["profile_text"])
        self.assertIn("future-only", profile["production_profile_text"])

    def test_warm_profile_reserves_a_paper_backed_temporal_prototype(self) -> None:
        venue = _venue()
        evidence = catalog_evidence(venue, self.policy.cutoff)
        papers = [
            _paper(
                "crossref",
                f"paper:{index}",
                title=f"Historical paper {index}",
                abstract="paper evidence",
            )
            for index in range(5)
        ]
        evidence.extend(papers)
        catalog_only_pcl = {
            "prototype_id": f"{venue.venue_id}:pcl:0",
            "kind": "historical_topic",
            "label": "Catalog-only result",
            "text": "catalog subject only",
            "keywords": [],
            "weight": 1.0,
            "confidence": "high",
            "source_ids": [f"catalog:{venue.venue_id}"],
            "source_max_date": self.policy.cutoff,
            "temporal_eligible": True,
            "derived_by": "pcl_llm",
            "model": "fake",
        }
        profile = build_venue_profile(
            venue,
            evidence,
            [catalog_only_pcl],
            cutoff=self.policy.cutoff,
            pcl_status="ok",
            pcl_model="fake",
            max_prototypes=2,
            collection_status="complete",
        )
        paper_ids = {str(row["evidence_id"]) for row in papers}
        self.assertEqual(profile["metadata"]["profile_tier"], "warm")
        self.assertTrue(
            any(
                paper_ids.intersection(prototype["source_ids"])
                for prototype in profile["research_prototypes"]
            )
        )
        self.assertGreater(
            profile["metadata"]["paper_backed_temporal_prototype_count"], 0
        )

    def test_clean_rebuild_separates_production_from_paper_research(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            (source / "venues").mkdir(parents=True)
            output = root / "clean-paper"
            jcr = root / "jcr.csv"
            jcr.write_text("fixture\n", encoding="utf-8")
            (source / "manifest.json").write_text(
                json.dumps({"schema_version": 2}), encoding="utf-8"
            )
            venue = replace(
                _venue(), online_entity_id=7, identity_status="exact_issn"
            )
            evidence = catalog_evidence(venue, self.policy.cutoff)
            paper = _paper(
                "crossref",
                "title:legacy-collision",
                title="한국어 암 영상 진단",
                abstract="historical paper evidence",
            )
            evidence.append(paper)
            evidence.append(
                {
                    "evidence_id": "official:future",
                    "venue_id": venue.venue_id,
                    "kind": "official_scope",
                    "source": "search:tavily",
                    "text": "future-only scope",
                    "valid_at": "2026-08-14",
                    "temporal_eligible": False,
                }
            )
            source_profile = build_venue_profile(
                venue,
                evidence,
                (),
                cutoff=self.policy.cutoff,
                pcl_status="ok",
                pcl_model="legacy",
                max_prototypes=6,
                collection_status="complete",
            )
            (source / "venues" / f"{venue.venue_id}.json").write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "evidence": evidence,
                        "profile": source_profile,
                    }
                ),
                encoding="utf-8",
            )
            manifest = rebuild_clean_corpus(
                venues=[venue],
                policy=self.policy,
                source_dir=source,
                output_dir=output,
                jcr_csv=jcr,
                mode="deterministic",
                workers=1,
            )
            retry_pcl = SequencedPCL([TimeoutError("transient"), "ok"])
            pcl_output = root / "clean-pcl"
            pcl_manifest = rebuild_clean_corpus(
                venues=[venue],
                policy=self.policy,
                source_dir=source,
                output_dir=pcl_output,
                jcr_csv=jcr,
                mode="pcl",
                pcl=retry_pcl,
                workers=1,
                pcl_attempts=2,
                pcl_backoff_base=0,
                pcl_backoff_max=0,
            )
            production = (output / "production_evidence.jsonl").read_text(
                encoding="utf-8"
            )
            research = (output / "research_evidence.jsonl").read_text(
                encoding="utf-8"
            )
            profile = json.loads(
                (output / "venue_profiles.train.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[0]
            )
            pcl_generation_count = len(
                (pcl_output / "pcl_generation.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            )

        self.assertIn("future-only", production)
        self.assertNotIn("future-only", research)
        self.assertIn(f"paper:{venue.venue_id}:title:", research)
        self.assertNotIn("future-only", profile["profile_text"])
        self.assertEqual(
            manifest["validation"][
                "warm_few_without_paper_backed_prototype_count"
            ],
            0,
        )
        self.assertFalse(output.with_name(f".{output.name}.building").exists())
        self.assertEqual(retry_pcl.calls, 2)
        self.assertEqual(
            pcl_manifest["validation"]["missing_prototype_source_id_count"], 0
        )
        self.assertEqual(pcl_generation_count, 1)

    def test_clean_rebuild_bounds_inflight_work_after_shared_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "manifest.json").write_text("{}", encoding="utf-8")
            jcr = root / "jcr.csv"
            jcr.write_text("fixture\n", encoding="utf-8")
            venues = [
                _venue(f"jcr-{index}", f"Journal {index}") for index in range(20)
            ]
            calls: list[str] = []
            call_lock = threading.Lock()

            def fail_first(venue, **kwargs):
                del kwargs
                with call_lock:
                    calls.append(venue.venue_id)
                if venue.venue_id == "jcr-0":
                    raise ConnectionError("shared provider unavailable")
                time.sleep(0.05)
                return {"venue_id": venue.venue_id}

            with patch(
                "research.clean_corpus._process_venue", side_effect=fail_first
            ):
                with self.assertRaisesRegex(
                    ConnectionError, "shared provider unavailable"
                ):
                    rebuild_clean_corpus(
                        venues=venues,
                        policy=self.policy,
                        source_dir=source,
                        output_dir=root / "clean",
                        jcr_csv=jcr,
                        mode="pcl",
                        pcl=FakePCL(),
                        workers=3,
                    )

        self.assertLessEqual(len(calls), 3)

    def test_scope_source_falls_back_to_direct_official_page_when_search_is_blocked(self) -> None:
        config = {
            "llm": {
                "base_url": "https://llmapi.pcl.ac.cn/v1",
                "model": "fake",
            },
            "search": {"provider": "tavily", "api_key": "test"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = OfficialScopeSearchSource(config, Path(temporary))
            with patch(
                "research.historical_builder.enrichment.enrich_row",
                return_value=(0, "error:RuntimeError", None, "HTTP 432"),
            ), patch(
                "research.historical_builder.enrichment.candidate_pages",
                return_value=[
                    PageText(
                        url="https://publisher.example/journal",
                        title="Aims and scope",
                        text="Official journal scope evidence " * 10,
                        links=[],
                    )
                ],
            ), patch(
                "research.historical_builder.enrichment.call_llm",
                return_value={
                    "is_relevant": True,
                    "scope_summary": "verified scope",
                    "scope_keywords": ["history"],
                    "source_url": "https://publisher.example/journal",
                    "evidence": "official scope",
                },
            ):
                rows = source.fetch(_venue(), self.policy)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "direct_official_page:pcl")
        self.assertFalse(rows[0]["temporal_eligible"])

    def test_assembly_keeps_unprocessed_candidates_as_explicit_cold_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jcr = root / "jcr.csv"
            jcr.write_text("name\nfixture\n", encoding="utf-8")
            first = replace(
                _venue("jcr-one", "One"),
                online_entity_id=7,
                identity_status="exact_issn",
            )
            second = _venue("jcr-two", "Two")
            result = process_venue(
                first,
                policy=self.policy,
                crossref=FakeSource("crossref", [_paper("crossref", "crossref:1")]),
                openalex=None,
                scope_search=None,
                pcl=FakePCL(),
            )
            venues_dir = root / "out" / "venues"
            venues_dir.mkdir(parents=True)
            (venues_dir / "jcr-one.json").write_text(json.dumps(result), encoding="utf-8")
            manifest = assemble_historical_corpus(
                venues=[first, second],
                policy=self.policy,
                output_dir=root / "out",
                jcr_csv=jcr,
                provider_identity=FakePCL.provider_identity,
            )
            profiles = [
                json.loads(line)
                for line in (root / "out" / "venue_profiles.train.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(len(profiles), 2)
            pending = next(row for row in profiles if row["venue_id"] == "jcr-two")
            self.assertEqual(pending["metadata"]["collection_status"], "pending")
            self.assertEqual(pending["metadata"]["profile_tier"], "cold")
            self.assertEqual(manifest["coverage"]["catalog_venues"], 2)
            custom_kg = json.loads(
                (root / "out" / "lightrag_custom_kg.json").read_text(encoding="utf-8")
            )
            self.assertTrue(
                any(row["entity_name"].startswith("VENUE::7::") for row in custom_kg["entities"])
            )
            self.assertTrue(custom_kg["relationships"])

    def test_queue_is_deterministic_and_independent_of_input_order(self) -> None:
        venues = [_venue(f"jcr-{index}", str(index)) for index in range(8)]
        first = [row.venue_id for row in stable_collection_queue(venues, "seed")]
        second = [row.venue_id for row in stable_collection_queue(list(reversed(venues)), "seed")]
        self.assertEqual(first, second)


class PCLRetryQueueTests(unittest.TestCase):
    def test_retry_schedule_and_attempt_survive_consecutive_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attempt_started = threading.Event()
            handler_calls: list[str] = []

            def handler(venue_id: str) -> PCLRetryOutcome:
                handler_calls.append(venue_id)
                attempt_started.set()
                return PCLRetryOutcome(
                    ok=False,
                    status="error:TimeoutError",
                    error="offline timeout",
                    retryable=True,
                )

            retry_policy = PCLRetryPolicy(
                max_attempts=3,
                second_pass_attempts=0,
                backoff_base=30,
                backoff_max=30,
                workers=1,
            )
            first_queue = PCLRetryQueue(root, handler, retry_policy)
            try:
                self.assertTrue(first_queue.enqueue("jcr-test"))
                self.assertTrue(attempt_started.wait(timeout=1))
                deadline = time.monotonic() + 1
                while first_queue.snapshot()["retried"] != 1:
                    if time.monotonic() >= deadline:
                        self.fail("retrying event was not persisted")
                    time.sleep(0.005)
            finally:
                first_queue.close(drain=False)

            first_events = [
                json.loads(line)
                for line in (root / "pcl_retry_queue.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            retrying = next(
                row for row in reversed(first_events) if row.get("event") == "retrying"
            )
            self.assertTrue(retrying["next_attempt_at"])
            self.assertGreater(retrying["next_attempt_epoch"], time.time())
            self.assertEqual(retrying["pass_number"], 1)
            self.assertEqual(retrying["attempts_completed"], 1)

            for _restart in range(2):
                restarted_queue = PCLRetryQueue(root, handler, retry_policy)
                try:
                    self.assertTrue(restarted_queue.enqueue("jcr-test"))
                finally:
                    restarted_queue.close(drain=False)

            all_events = [
                json.loads(line)
                for line in (root / "pcl_retry_queue.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            restart_events = [
                row
                for row in all_events
                if row.get("event") == "requeued_after_restart"
            ]

        self.assertEqual(handler_calls, ["jcr-test"])
        self.assertEqual(len(restart_events), 2)
        for row in restart_events:
            self.assertEqual(row["pass_number"], 1)
            self.assertEqual(row["attempts_completed"], 1)
            self.assertEqual(row["attempt"], 1)
            self.assertTrue(row["next_attempt_at"])
            self.assertGreater(row["next_attempt_epoch"], 0)


class PrototypeRetrievalTests(unittest.TestCase):
    def test_jsonl_loader_and_tfidf_use_temporal_prototypes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profiles.jsonl"
            rows = [
                {
                    "venue_id": "v1",
                    "name": "Broad Journal",
                    "profile_text": "broad science",
                    "snapshot_date": "2026-03-31",
                    "prototypes": [
                        {"text": "quantum graph methods", "temporal_eligible": True},
                        {"text": "forbidden future phrase", "temporal_eligible": False},
                    ],
                    "metadata": {"paper_count": 5},
                },
                {
                    "venue_id": "v2",
                    "name": "Other Journal",
                    "profile_text": "robot ethics",
                    "snapshot_date": "2026-03-31",
                    "prototypes": [{"text": "robot ethics", "temporal_eligible": True}],
                    "metadata": {"paper_count": 0},
                },
            ]
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            corpus = load_jsonl_corpus(path, text_fields=("name", "profile_text"))
            run = TfidfBaseline(use_prototypes=True).fit(corpus).run(
                [Query("q", "quantum graph", "2026-06-01")], top_k=2
            )
            self.assertEqual(run["q"][0].doc_id, "v1")
            self.assertIn("prototypes", corpus[0].metadata)

    def test_pcl_vector_run_max_pools_prototypes_to_venue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profiles = root / "profiles.jsonl"
            profiles.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in [
                        {
                            "venue_id": "v1",
                            "profile_text": "broad",
                            "prototypes": [
                                {"prototype_id": "v1:a", "text": "medical cancer", "weight": 1},
                                {"prototype_id": "v1:b", "text": "quantum graph", "weight": 1},
                            ],
                        },
                        {
                            "venue_id": "v2",
                            "profile_text": "robot ethics",
                            "prototypes": [
                                {"prototype_id": "v2:a", "text": "robot ethics", "weight": 1}
                            ],
                        },
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            query = Query("q1", "quantum graph", "2026-06-01")
            bundle = DatasetBundle(
                queries=(query,),
                qrels={"q1": {"v1": 1.0}},
                source_rows={"q1": {}},
            )
            output = root / "run.jsonl"
            manifest = build_prototype_vector_run(
                provider=FakeEmbeddingProvider(),
                bundle=bundle,
                profiles_path=profiles,
                cache_path=root / "vectors.json.gz",
                output_path=output,
                top_k=2,
                query_batch_size=1,
                prototype_chunk_size=2,
            )
            first = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(first["venue_id"], "v1")
            self.assertEqual(manifest["prototype_count"], 3)
            self.assertEqual(manifest["venue_count"], 2)


if __name__ == "__main__":
    unittest.main()
