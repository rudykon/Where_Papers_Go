from __future__ import annotations

import io
import http.client
import json
import os
import socket
import stat
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import scripts.build_recent_journal_benchmark as benchmark_builder
import scripts.evaluate_recent_journals as benchmark_evaluator

from scripts.build_recent_journal_benchmark import (
    BuildWindow,
    CrossrefClient,
    CrossrefItemEvidence,
    CrossrefResponseEvidence,
    JournalVenue,
    _RejectCrossrefRedirects,
    allocate_stratum_targets,
    _finalize_benchmark_outputs,
    _parse_request_ledger_lines,
    _record_with_item_evidence,
    _sha256_file,
    _verify_acquisition_evidence,
    build_issn_index,
    classify_broad_field,
    jats_to_text,
    prepare_crossref_record,
    build_parser,
    plan_benchmark,
    resolve_item_venue,
    select_records_for_journal,
    select_bulk_records,
    stratified_journal_order,
)


def make_venue(
    venue_id: str,
    issn: str,
    *,
    field: str = "clinical_medicine",
    quartile: str = "Q1",
) -> JournalVenue:
    token = issn.replace("-", "")
    return JournalVenue(
        venue_id=venue_id,
        entity_id=100,
        name=f"Journal {venue_id}",
        quartile=quartile,
        category="ONCOLOGY",
        broad_field=field,
        issns=(token,),
        lookup_issn=issn,
    )


def make_item(**overrides):
    item = {
        "DOI": "10.1234/example",
        "type": "journal-article",
        "title": ["A useful original research article"],
        "abstract": "<jats:p>" + ("Detailed findings and methods. " * 20) + "</jats:p>",
        "ISSN": ["0007-9235"],
        "container-title": ["Example Journal"],
        "published-online": {"date-parts": [[2026, 2, 3]]},
        "language": "en",
    }
    item.update(overrides)
    return item


class TextCleanupTests(unittest.TestCase):
    def test_jats_is_converted_to_normalized_plain_text(self) -> None:
        value = (
            "<jats:abstract><jats:title>Abstract</jats:title>"
            "<jats:p>A &amp; B <jats:italic>result</jats:italic>.</jats:p>"
            "<jats:p>Second&nbsp;sentence.</jats:p></jats:abstract>"
        )
        self.assertEqual(jats_to_text(value), "A & B result. Second sentence.")

    def test_broad_field_mapping_covers_distinct_disciplines(self) -> None:
        cases = {
            "COMPUTER SCIENCE, ARTIFICIAL INTELLIGENCE": "computer_engineering",
            "MATHEMATICS, APPLIED": "mathematics_statistics",
            "CHEMISTRY, PHYSICAL": "physical_chemical_materials",
            "ONCOLOGY": "clinical_medicine",
            "BIOCHEMISTRY & MOLECULAR BIOLOGY": "life_sciences",
            "ECOLOGY": "earth_environment_agriculture",
            "ECONOMICS": "social_sciences",
            "HISTORY & PHILOSOPHY OF SCIENCE": "arts_humanities",
            "MULTIDISCIPLINARY SCIENCES": "multidisciplinary_other",
        }
        for category, expected in cases.items():
            with self.subTest(category=category):
                self.assertEqual(classify_broad_field(category), expected)


class IssnResolutionTests(unittest.TestCase):
    def test_exact_issn_resolves_and_ambiguous_issn_is_rejected(self) -> None:
        first = make_venue("first", "0007-9235")
        second = make_venue("second", "1471-0072")
        index = build_issn_index([first, second])
        resolved, status = resolve_item_venue({"ISSN": ["0007-9235"]}, index)
        self.assertEqual((resolved, status), (first, "ok"))

        ambiguous = build_issn_index([first, make_venue("duplicate", "0007-9235")])
        resolved, status = resolve_item_venue({"ISSN": ["0007-9235"]}, ambiguous)
        self.assertIsNone(resolved)
        self.assertEqual(status, "ambiguous_issn")

    def test_unmapped_item_is_not_name_matched(self) -> None:
        index = build_issn_index([make_venue("known", "0007-9235")])
        resolved, status = resolve_item_venue(
            {"ISSN": ["1471-0072"], "container-title": ["Journal known"]},
            index,
        )
        self.assertIsNone(resolved)
        self.assertEqual(status, "unmapped_issn")


class RecordPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.venue = make_venue("known", "0007-9235")
        self.index = build_issn_index([self.venue])
        self.window = BuildWindow(date(2026, 1, 1), date(2026, 4, 30))

    def prepare(self, item):
        return prepare_crossref_record(
            item,
            issn_index=self.index,
            expected_venue=self.venue,
            window=self.window,
            min_abstract_chars=100,
        )

    def test_valid_article_becomes_labeled_model_input(self) -> None:
        record, status = self.prepare(make_item())
        self.assertEqual(status, "ok")
        self.assertEqual(record["gold_journal_id"], "known")
        self.assertEqual(record["gold_entity_id"], 100)
        self.assertEqual(record["gold_jcr_quartile"], "Q1")
        self.assertEqual(record["source_url"], "https://doi.org/10.1234/example")
        self.assertNotIn("<jats:p>", record["abstract"])
        self.assertEqual(record["publication_date"], "2026-02-03")
        self.assertEqual(record["publication_date_precision"], "day")

    def test_title_markup_is_removed_and_year_only_date_is_rejected(self) -> None:
        record, status = self.prepare(
            make_item(title=["<scp>DMV</scp> - <i>CLIP</i>"])
        )
        self.assertEqual(status, "ok")
        self.assertEqual(record["title"], "DMV - CLIP")

        record, status = self.prepare(
            make_item(**{"published-online": {"date-parts": [[2026]]}})
        )
        self.assertIsNone(record)
        self.assertEqual(status, "imprecise_publication_date")

    def test_short_abstract_notice_and_wrong_type_are_rejected(self) -> None:
        cases = (
            (make_item(abstract="<jats:p>Too short.</jats:p>"), "short_abstract"),
            (make_item(title=["Correction to an earlier paper"]), "non_article_notice"),
            (make_item(type="posted-content"), "non_article_notice"),
            (make_item(ISSN=["1471-0072"]), "unmapped_issn"),
            (
                make_item(**{"published-online": {"date-parts": [[2025, 12, 31]]}}),
                "outside_date_window",
            ),
        )
        for item, expected in cases:
            with self.subTest(expected=expected):
                record, status = self.prepare(item)
                self.assertIsNone(record)
                self.assertEqual(status, expected)


class DeterministicSamplingTests(unittest.TestCase):
    def test_bulk_selection_enforces_one_paper_per_journal(self) -> None:
        selected = select_bulk_records(
            {
                "a": [
                    {"doi": "10.1/a1", "paper_id": "doi:10.1/a1"},
                    {"doi": "10.1/a2", "paper_id": "doi:10.1/a2"},
                ],
                "b": [{"doi": "10.1/b1", "paper_id": "doi:10.1/b1"}],
            },
            limit=2,
            max_papers_per_journal=1,
            seed="fixed",
            stratum=("field", "Q1"),
        )
        self.assertEqual(len(selected), 2)
        self.assertEqual(len({row["doi"].rsplit("/", 1)[-1][0] for row in selected}), 2)

    def test_small_total_is_balanced_across_fields_and_quartiles(self) -> None:
        fields = [f"field_{index}" for index in range(9)]
        strata = [(field, quartile) for field in fields for quartile in ("Q1", "Q2", "Q3", "Q4")]
        targets = allocate_stratum_targets(
            strata, sample_size=20, samples_per_stratum=99
        )
        self.assertEqual(sum(targets.values()), 20)
        self.assertTrue(all(sum(targets[field, q] for q in ("Q1", "Q2", "Q3", "Q4")) >= 2 for field in fields))
        quartile_counts = [sum(targets[field, q] for field in fields) for q in ("Q1", "Q2", "Q3", "Q4")]
        self.assertLessEqual(max(quartile_counts) - min(quartile_counts), 1)

    def test_stratified_order_and_record_cap_are_deterministic(self) -> None:
        venues = [
            make_venue("a", "0007-9235", field="clinical_medicine", quartile="Q1"),
            make_venue("b", "1471-0072", field="clinical_medicine", quartile="Q1"),
        ]
        kwargs = {
            "fields": {"clinical_medicine"},
            "quartiles": {"Q1"},
            "seed": "fixed",
        }
        first = stratified_journal_order(venues, **kwargs)
        second = stratified_journal_order(reversed(venues), **kwargs)
        self.assertEqual(first, second)

        records = [
            {"doi": "10.1/c", "paper_id": "doi:10.1/c"},
            {"doi": "10.1/a", "paper_id": "doi:10.1/a"},
            {"doi": "10.1/b", "paper_id": "doi:10.1/b"},
        ]
        selected = select_records_for_journal(
            records, limit=2, seed="fixed", venue_id="a"
        )
        repeated = select_records_for_journal(
            reversed(records), limit=2, seed="fixed", venue_id="a"
        )
        self.assertEqual(selected, repeated)
        self.assertEqual(len(selected), 2)


class PlanningTests(unittest.TestCase):
    def test_plan_is_zero_network_and_does_not_create_output(self) -> None:
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "future"
            args = build_parser().parse_args(
                [
                    "--output-dir",
                    str(output),
                    "--from-date",
                    "2026-07-01",
                    "--until-date",
                    "2026-07-31",
                    "--sample-size",
                    "36",
                    "--bulk-pages",
                    "2",
                    "--journal-attempt-multiplier",
                    "1",
                    "--max-network-requests",
                    "40",
                    "--plan-only",
                ]
            )
            plan = plan_benchmark(args)
            self.assertFalse(plan["network_performed"])
            self.assertEqual(plan["selection"]["target_records"], 36)
            self.assertEqual(plan["request_bound"]["bulk_logical_requests"], 2)
            self.assertEqual(plan["request_bound"]["configured_http_attempt_cap"], 40)
            self.assertEqual(plan["external_cost"]["estimated_charge_usd"], 0.0)
            self.assertFalse(plan["acquisition_evidence"]["required"])
            self.assertFalse(output.exists())


class CrossrefRequestSafetyTests(unittest.TestCase):
    def test_ledger_parser_rejects_retroactive_malformed_records(self) -> None:
        malformed = {
            "schema_version": 1,
            "event": "attempt_reserved",
            "sequence": 1,
            "budget_id": "strict-budget",
            "reserved_at": "not-a-date",
            "request_url_sha256": "Z" * 64,
            "recorded_after_fact": True,
            "unexpected": "field",
        }
        with self.assertRaisesRegex(ValueError, "continuity mismatch"):
            _parse_request_ledger_lines(
                [json.dumps(malformed) + "\n"],
                path=Path("malformed-ledger.jsonl"),
                budget_id="strict-budget",
            )

    def test_socket_is_opened_only_after_durable_reservation(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = root / "request-ledger.jsonl"

            class InspectingOpener:
                def open(self, _request, *, timeout):
                    self.timeout = timeout
                    rows = [
                        json.loads(line)
                        for line in ledger.read_text(encoding="utf-8").splitlines()
                    ]
                    if len(rows) != 1 or rows[0]["event"] != "attempt_reserved":
                        raise AssertionError("socket observed before reservation")
                    return io.BytesIO(b'{"message":{"items":[]}}')

            client = CrossrefClient(
                cache_dir=root / "cache",
                mailto="test@example.org",
                timeout=5.0,
                retries=0,
                request_interval=0.0,
                use_environment_proxy=False,
                refresh_cache=False,
                max_network_requests=1,
                request_ledger=ledger,
                request_budget_id="pre-socket-budget",
            )
            client.opener = InspectingOpener()
            client.get_json("/works", {"cursor": "first", "rows": "1"})

    def test_default_opener_rejects_redirect_hops(self) -> None:
        with TemporaryDirectory() as temporary:
            client = CrossrefClient(
                cache_dir=Path(temporary) / "cache",
                mailto="test@example.org",
                timeout=5.0,
                retries=0,
                request_interval=0.0,
                use_environment_proxy=False,
                refresh_cache=False,
                max_network_requests=1,
            )
            handlers = [
                handler
                for handler in client.opener.handlers
                if isinstance(handler, _RejectCrossrefRedirects)
            ]
            self.assertEqual(len(handlers), 1)
            self.assertEqual(
                sum(
                    isinstance(handler, urllib.request.HTTPRedirectHandler)
                    for handler in client.opener.handlers
                ),
                1,
            )
            with self.assertRaisesRegex(ValueError, "redirect refused"):
                handlers[0].redirect_request(
                    None, None, 302, "Found", {}, "https://example.invalid/next"
                )

    def test_cursor_page_omits_crossref_incompatible_published_sort(self) -> None:
        captured: dict[str, object] = {}
        client = object.__new__(CrossrefClient)

        def fake_get_json(path: str, params: dict[str, str]):
            captured["path"] = path
            captured["params"] = params
            return {"message": {"items": [], "next-cursor": "next"}}

        client.get_json = fake_get_json
        items, cursor = CrossrefClient.works_page(
            client,
            window=BuildWindow(date(2026, 7, 1), date(2026, 7, 31)),
            rows=1000,
        )
        self.assertEqual(items, [])
        self.assertEqual(cursor, "next")
        self.assertEqual(captured["path"], "/works")
        params = captured["params"]
        self.assertIsInstance(params, dict)
        self.assertNotIn("sort", params)
        self.assertNotIn("order", params)
        self.assertEqual(params["cursor"], "*")

    def test_evidence_cursor_page_uses_the_same_fixed_protocol(self) -> None:
        captured: dict[str, object] = {}
        client = object.__new__(CrossrefClient)

        def fake_get_json_with_evidence(path: str, params: dict[str, str]):
            captured["path"] = path
            captured["params"] = params
            return (
                {"message": {"items": [make_item()], "next-cursor": "next"}},
                CrossrefResponseEvidence(
                    request_url_sha256="a" * 64,
                    cache_relative_path=("a" * 64) + ".json",
                    response_sha256="b" * 64,
                    response_bytes=123,
                    observed_via="cache",
                    request_descriptor={
                        "schema_version": 1,
                        "base_url": "https://api.crossref.org",
                        "path": "/works",
                        "query": {
                            "cursor": "*",
                            "filter": (
                                "from-pub-date:2026-07-01,"
                                "until-pub-date:2026-07-31,"
                                "type:journal-article,has-abstract:true"
                            ),
                            "mailto": "test@example.org",
                            "rows": "1000",
                        },
                    },
                ),
            )

        client.get_json_with_evidence = fake_get_json_with_evidence
        items, cursor = CrossrefClient.works_page_with_evidence(
            client,
            window=BuildWindow(date(2026, 7, 1), date(2026, 7, 31)),
            rows=1000,
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].item_index, 0)
        self.assertEqual(cursor, "next")
        self.assertEqual(captured["path"], "/works")
        params = captured["params"]
        self.assertIsInstance(params, dict)
        self.assertNotIn("sort", params)
        self.assertNotIn("order", params)
        self.assertEqual(params["cursor"], "*")

    def test_append_only_ledger_enforces_cap_across_clients(self) -> None:
        class FakeOpener:
            def open(self, _request, *, timeout):
                self.timeout = timeout
                return io.BytesIO(b'{"message":{"items":[]}}')

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = root / "request-ledger.jsonl"

            def make_client() -> CrossrefClient:
                client = CrossrefClient(
                    cache_dir=root / "cache",
                    mailto="test@example.org",
                    timeout=5.0,
                    retries=0,
                    request_interval=0.0,
                    use_environment_proxy=False,
                    refresh_cache=False,
                    max_network_requests=2,
                    request_ledger=ledger,
                    request_budget_id="fixed-budget",
                )
                client.opener = FakeOpener()
                return client

            first = make_client()
            first.get_json("/works", {"cursor": "first", "rows": "1"})
            first.get_json("/works", {"cursor": "second", "rows": "1"})
            self.assertEqual(first.network_requests, 2)
            self.assertEqual(first.cumulative_network_requests, 2)

            rows = [json.loads(line) for line in ledger.read_text().splitlines()]
            self.assertEqual([row["sequence"] for row in rows], [1, 2])
            self.assertTrue(all(not row["recorded_after_fact"] for row in rows))

            resumed = make_client()
            self.assertEqual(resumed.cumulative_network_requests, 2)
            with self.assertRaisesRegex(ValueError, "cumulative.*exhausted"):
                resumed.get_json("/works", {"cursor": "third", "rows": "1"})
            self.assertEqual(resumed.network_requests, 0)
            self.assertEqual(len(ledger.read_text().splitlines()), 2)

    def test_same_inode_ledger_truncation_cannot_restore_spent_allowance(self) -> None:
        class CountingOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, _request, *, timeout):
                self.timeout = timeout
                self.calls += 1
                return io.BytesIO(b'{"message":{"items":[]}}')

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = root / "request-ledger.jsonl"
            registry = root / "registry"
            opener = CountingOpener()

            def make_client() -> CrossrefClient:
                client = CrossrefClient(
                    cache_dir=root / "cache",
                    mailto="test@example.org",
                    timeout=5.0,
                    retries=0,
                    request_interval=0.0,
                    use_environment_proxy=False,
                    refresh_cache=False,
                    max_network_requests=2,
                    request_ledger=ledger,
                    request_budget_id="truncate-budget",
                    require_private_storage=True,
                    budget_registry_dir=registry,
                )
                client.opener = opener
                return client

            client = make_client()
            client.get_json("/works", {"cursor": "first", "rows": "1"})
            client.get_json("/works", {"cursor": "second", "rows": "1"})
            original_inode = ledger.stat().st_ino
            with ledger.open("w", encoding="utf-8") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            self.assertEqual(ledger.stat().st_ino, original_inode)
            with self.assertRaisesRegex(ValueError, "rolled back|diverged"):
                make_client()
            self.assertEqual(opener.calls, 2)
            self.assertEqual(
                len(client.request_highwater_path.read_text().splitlines()), 2
            )
            self.assertEqual(len(client.global_usage_path.read_text().splitlines()), 2)

    def test_active_client_rejects_ledger_inode_replacement_before_socket(self) -> None:
        class CountingOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, _request, *, timeout):
                self.timeout = timeout
                self.calls += 1
                return io.BytesIO(b'{"message":{"items":[]}}')

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = root / "request-ledger.jsonl"
            client = CrossrefClient(
                cache_dir=root / "cache",
                mailto="test@example.org",
                timeout=5.0,
                retries=0,
                request_interval=0.0,
                use_environment_proxy=False,
                refresh_cache=False,
                max_network_requests=2,
                request_ledger=ledger,
                request_budget_id="replace-budget",
            )
            opener = CountingOpener()
            client.opener = opener
            client.get_json("/works", {"cursor": "first", "rows": "1"})
            replacement = root / "replacement.jsonl"
            replacement.write_bytes(ledger.read_bytes())
            os.chmod(replacement, 0o600)
            os.replace(replacement, ledger)
            with self.assertRaisesRegex(ValueError, "budget binding mismatch"):
                client.get_json("/works", {"cursor": "second", "rows": "1"})
            self.assertEqual(opener.calls, 1)

    def test_crash_between_reservation_anchors_is_permanently_fail_closed(self) -> None:
        class CountingOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, _request, *, timeout):
                self.timeout = timeout
                self.calls += 1
                return io.BytesIO(b'{"message":{"items":[]}}')

        for fault_after in (1, 2):
            with self.subTest(fault_after=fault_after), TemporaryDirectory() as temporary:
                root = Path(temporary)
                ledger = root / "request-ledger.jsonl"
                registry = root / "registry"
                opener = CountingOpener()

                def make_client() -> CrossrefClient:
                    client = CrossrefClient(
                        cache_dir=root / "cache",
                        mailto="test@example.org",
                        timeout=5.0,
                        retries=0,
                        request_interval=0.0,
                        use_environment_proxy=False,
                        refresh_cache=False,
                        max_network_requests=2,
                        request_ledger=ledger,
                        request_budget_id=f"crash-budget-{fault_after}",
                        require_private_storage=True,
                        budget_registry_dir=registry,
                    )
                    client.opener = opener
                    return client

                client = make_client()
                client._reservation_fault_after_writes = fault_after
                with self.assertRaisesRegex(RuntimeError, "injected crash"):
                    client.get_json("/works", {"cursor": "first", "rows": "1"})
                self.assertEqual(opener.calls, 0)
                self.assertEqual(len(client.global_usage_path.read_text().splitlines()), 1)
                self.assertEqual(
                    len(client.request_highwater_path.read_text().splitlines()),
                    1 if fault_after == 2 else 0,
                )
                self.assertEqual(len(ledger.read_text().splitlines()), 0)
                with self.assertRaisesRegex(ValueError, "rolled back|diverged"):
                    make_client()
                self.assertEqual(opener.calls, 0)

    def test_immutable_budget_binding_rejects_ceiling_expansion_or_change(self) -> None:
        class FakeOpener:
            def open(self, _request, *, timeout):
                self.timeout = timeout
                return io.BytesIO(b'{"message":{"items":[]}}')

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = root / "request-ledger.jsonl"

            def make_client(ceiling: int) -> CrossrefClient:
                client = CrossrefClient(
                    cache_dir=root / "cache",
                    mailto="test@example.org",
                    timeout=5.0,
                    retries=0,
                    request_interval=0.0,
                    use_environment_proxy=False,
                    refresh_cache=False,
                    max_network_requests=ceiling,
                    request_ledger=ledger,
                    request_budget_id="immutable-budget",
                )
                client.opener = FakeOpener()
                return client

            first = make_client(2)
            first.get_json("/works", {"cursor": "first", "rows": "1"})
            self.assertEqual(make_client(2).cumulative_network_requests, 1)
            for changed_ceiling in (1, 3, 1000):
                with self.subTest(changed_ceiling=changed_ceiling):
                    with self.assertRaisesRegex(ValueError, "budget binding mismatch"):
                        make_client(changed_ceiling)
            binding = ledger.with_name(ledger.name + ".budget.json")
            self.assertEqual(stat.S_IMODE(binding.stat().st_mode), 0o400)
            persisted = json.loads(binding.read_text(encoding="utf-8"))
            self.assertEqual(persisted["hard_http_attempt_ceiling"], 2)
            replacement = root / "replacement-ledger.jsonl"
            replacement.write_bytes(b"")
            os.chmod(replacement, 0o600)
            os.replace(replacement, ledger)
            with self.assertRaisesRegex(ValueError, "ledger identity"):
                make_client(2)

    def test_nonempty_legacy_ledger_cannot_be_retroactively_bound(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = root / "legacy.jsonl"
            ledger.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "event": "attempt_reserved",
                        "sequence": 1,
                        "budget_id": "legacy-budget",
                        "reserved_at": "2026-08-30T00:00:00+00:00",
                        "request_url_sha256": "a" * 64,
                        "recorded_after_fact": False,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "cannot be adopted retroactively"):
                CrossrefClient(
                    cache_dir=root / "cache",
                    mailto="test@example.org",
                    timeout=5.0,
                    retries=0,
                    request_interval=0.0,
                    use_environment_proxy=False,
                    refresh_cache=False,
                    max_network_requests=1000,
                    request_ledger=ledger,
                    request_budget_id="legacy-budget",
                )

    def test_global_budget_claim_rejects_new_ledger_and_missing_ledger_reset(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = root / "registry"
            first_ledger = root / "first" / "requests.jsonl"
            CrossrefClient(
                cache_dir=root / "cache-one",
                mailto="test@example.org",
                timeout=5.0,
                retries=0,
                request_interval=0.0,
                use_environment_proxy=False,
                refresh_cache=False,
                max_network_requests=2,
                request_ledger=first_ledger,
                request_budget_id="global-budget",
                require_private_storage=True,
                budget_registry_dir=registry,
            )
            with self.assertRaisesRegex(
                ValueError, "missing behind an immutable budget|budget binding mismatch"
            ):
                CrossrefClient(
                    cache_dir=root / "cache-two",
                    mailto="test@example.org",
                    timeout=5.0,
                    retries=0,
                    request_interval=0.0,
                    use_environment_proxy=False,
                    refresh_cache=False,
                    max_network_requests=2,
                    request_ledger=root / "second" / "requests.jsonl",
                    request_budget_id="global-budget",
                    require_private_storage=True,
                    budget_registry_dir=registry,
                )
            first_ledger.unlink()
            with self.assertRaisesRegex(ValueError, "missing behind an immutable budget"):
                CrossrefClient(
                    cache_dir=root / "cache-three",
                    mailto="test@example.org",
                    timeout=5.0,
                    retries=0,
                    request_interval=0.0,
                    use_environment_proxy=False,
                    refresh_cache=False,
                    max_network_requests=2,
                    request_ledger=first_ledger,
                    request_budget_id="global-budget",
                    require_private_storage=True,
                    budget_registry_dir=registry,
                )

    def test_missing_global_claim_cannot_be_recreated_after_binding(self) -> None:
        class CountingOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, _request, *, timeout):
                self.timeout = timeout
                self.calls += 1
                return io.BytesIO(b'{"message":{"items":[]}}')

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = root / "request-ledger.jsonl"
            registry = root / "registry"
            opener = CountingOpener()

            def make_client() -> CrossrefClient:
                client = CrossrefClient(
                    cache_dir=root / "cache",
                    mailto="test@example.org",
                    timeout=5.0,
                    retries=0,
                    request_interval=0.0,
                    use_environment_proxy=False,
                    refresh_cache=False,
                    max_network_requests=2,
                    request_ledger=ledger,
                    request_budget_id="missing-claim-budget",
                    require_private_storage=True,
                    budget_registry_dir=registry,
                )
                client.opener = opener
                return client

            client = make_client()
            client.get_json("/works", {"cursor": "first", "rows": "1"})
            missing_claim_backup = root / "missing-claim-backup.json"
            os.replace(client.budget_registry_claim_path, missing_claim_backup)
            with self.assertRaisesRegex(ValueError, "refusing to recreate"):
                make_client()
            self.assertEqual(opener.calls, 1)

    def test_incomplete_chunked_response_is_retried_and_counted(self) -> None:
        class TransientOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, _request, *, timeout):
                self.calls += 1
                self.timeout = timeout
                if self.calls == 1:
                    raise http.client.IncompleteRead(b"partial")
                return io.BytesIO(b'{"message":{"items":[]}}')

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = root / "request-ledger.jsonl"
            client = CrossrefClient(
                cache_dir=root / "cache",
                mailto="test@example.org",
                timeout=5.0,
                retries=1,
                request_interval=0.0,
                use_environment_proxy=False,
                refresh_cache=False,
                max_network_requests=3,
                request_ledger=ledger,
                request_budget_id="transient-budget",
            )
            opener = TransientOpener()
            client.opener = opener
            payload = client.get_json("/works", {"cursor": "first", "rows": "1"})
            self.assertEqual(payload, {"message": {"items": []}})
            self.assertEqual(opener.calls, 2)
            self.assertEqual(client.network_requests, 2)
            self.assertEqual(client.cumulative_network_requests, 2)
            self.assertEqual(len(ledger.read_text().splitlines()), 2)

    def test_permanent_http_error_is_cached_without_spending_again(self) -> None:
        class NotFoundOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, request, *, timeout):
                self.calls += 1
                raise urllib.error.HTTPError(
                    request.full_url, 404, "Not Found", None, None
                )

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = root / "request-ledger.jsonl"

            def make_client() -> CrossrefClient:
                return CrossrefClient(
                    cache_dir=root / "cache",
                    mailto="test@example.org",
                    timeout=5.0,
                    retries=4,
                    request_interval=0.0,
                    use_environment_proxy=False,
                    refresh_cache=False,
                    max_network_requests=3,
                    request_ledger=ledger,
                    request_budget_id="permanent-error-budget",
                )

            first = make_client()
            opener = NotFoundOpener()
            first.opener = opener
            with self.assertRaises(urllib.error.HTTPError) as raised:
                first.get_json("/journals/0000-0000/works", {"rows": "1"})
            self.assertEqual(raised.exception.code, 404)
            raised.exception.close()
            self.assertEqual(opener.calls, 1)
            self.assertEqual(len(ledger.read_text().splitlines()), 1)
            error_files = list((root / "cache").glob("*.error.json"))
            self.assertEqual(len(error_files), 1)
            self.assertEqual(stat.S_IMODE(error_files[0].stat().st_mode), 0o400)
            self.assertEqual(stat.S_IMODE((root / "cache").stat().st_mode), 0o700)
            error_record = json.loads(error_files[0].read_text(encoding="utf-8"))
            self.assertNotIn("url", error_record)
            self.assertEqual(error_record["status"], 404)

            resumed = make_client()
            resumed.opener = NotFoundOpener()
            with self.assertRaises(urllib.error.HTTPError) as cached:
                resumed.get_json("/journals/0000-0000/works", {"rows": "1"})
            self.assertEqual(cached.exception.code, 404)
            cached.exception.close()
            self.assertEqual(resumed.opener.calls, 0)
            self.assertEqual(resumed.permanent_error_cache_hits, 1)
            self.assertEqual(len(ledger.read_text().splitlines()), 1)


class FailedBuildEvidenceTests(unittest.TestCase):
    def test_incomplete_build_is_persisted_before_failure(self) -> None:
        with TemporaryDirectory() as temporary:
            output = Path(temporary)
            records = [{"paper_id": "doi:10.1/partial", "title": "Partial"}]
            manifest = {
                "dataset": {
                    "path": "dataset.jsonl",
                    "record_count": 1,
                    "sha256": "pending",
                    "complete": False,
                }
            }
            with self.assertRaisesRegex(ValueError, "benchmark is incomplete"):
                _finalize_benchmark_outputs(
                    output,
                    records,
                    manifest,
                    allow_incomplete=False,
                )
            dataset = output / "dataset.jsonl"
            manifest_path = output / "manifest.json"
            self.assertTrue(dataset.is_file())
            self.assertTrue(manifest_path.is_file())
            persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertFalse(persisted["dataset"]["complete"])
            self.assertNotEqual(persisted["dataset"]["sha256"], "pending")

    def test_final_outputs_are_exclusive_and_never_overwritten(self) -> None:
        with TemporaryDirectory() as temporary:
            output = Path(temporary)
            first = [{"paper_id": "doi:10.1/first", "title": "First"}]
            manifest = {
                "dataset": {
                    "path": "dataset.jsonl",
                    "record_count": 1,
                    "sha256": "pending",
                    "complete": True,
                }
            }
            _finalize_benchmark_outputs(
                output, first, manifest, allow_incomplete=False
            )
            original_dataset = (output / "dataset.jsonl").read_bytes()
            original_manifest = (output / "manifest.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                _finalize_benchmark_outputs(
                    output,
                    [{"paper_id": "doi:10.1/second", "title": "Second"}],
                    {
                        "dataset": {
                            "path": "dataset.jsonl",
                            "record_count": 1,
                            "sha256": "pending",
                            "complete": True,
                        }
                    },
                    allow_incomplete=False,
                )
            self.assertEqual((output / "dataset.jsonl").read_bytes(), original_dataset)
            self.assertEqual((output / "manifest.json").read_bytes(), original_manifest)


class AcquisitionEvidenceTests(unittest.TestCase):
    @staticmethod
    def _fixture_issn(index: int) -> str:
        prefix = f"{1_000_000 + index:07d}"
        weighted = sum(
            int(value) * weight
            for value, weight in zip(prefix, range(8, 1, -1))
        )
        check = (11 - weighted % 11) % 11
        suffix = "X" if check == 10 else str(check)
        return f"{prefix[:4]}-{prefix[4:]}{suffix}"

    def test_complete_500_item_evidence_build_replays_entirely_offline(self) -> None:
        strata = [
            (field, quartile)
            for field in benchmark_builder.BROAD_FIELDS
            for quartile in benchmark_builder.QUARTILES
        ]
        targets = allocate_stratum_targets(
            strata,
            sample_size=500,
            samples_per_stratum=10,
        )
        venues: list[JournalVenue] = []
        items: list[dict[str, object]] = []
        item_number = 0
        for field, quartile in strata:
            for _ in range(targets[(field, quartile)]):
                issn = self._fixture_issn(item_number)
                venue = JournalVenue(
                    venue_id=f"offline-fixture-{item_number:03d}",
                    entity_id=10_000 + item_number,
                    name=f"Offline Fixture Journal {item_number:03d}",
                    quartile=quartile,
                    category="OFFLINE INTEGRATION FIXTURE",
                    broad_field=field,
                    issns=(issn.replace("-", ""),),
                    lookup_issn=issn,
                )
                venues.append(venue)
                items.append(
                    {
                        "DOI": f"10.9999/offline-evidence-{item_number:03d}",
                        "type": "journal-article",
                        "title": [
                            f"Offline acquisition evidence article {item_number:03d}"
                        ],
                        "abstract": (
                            "<jats:p>"
                            + ("Detailed offline methods and findings. " * 12)
                            + "</jats:p>"
                        ),
                        "ISSN": [issn],
                        "container-title": [venue.name],
                        "published-online": {"date-parts": [[2026, 2, 3]]},
                        "language": "en",
                    }
                )
                item_number += 1
        self.assertEqual(item_number, 500)
        self.assertEqual(sum(targets.values()), 500)

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache_dir = root / "cache"
            ledger = root / "request-ledger.jsonl"
            registry = root / "budget-registry"
            output = root / "formal-bundle"
            mailto = "offline500@example.org"
            budget_id = "offline-500-evidence-budget"
            window = BuildWindow(date(2026, 1, 1), date(2026, 6, 30))
            request_params = {
                "cursor": "*",
                "filter": (
                    "from-pub-date:2026-01-01,until-pub-date:2026-06-30,"
                    "type:journal-article,has-abstract:true"
                ),
                "rows": "500",
            }

            network_forbidden = AssertionError(
                "the 500-item acquisition-evidence integration test must stay offline"
            )
            with (
                mock.patch.object(
                    urllib.request.OpenerDirector,
                    "open",
                    side_effect=network_forbidden,
                ) as opener_open,
                mock.patch.object(
                    http.client.HTTPConnection,
                    "connect",
                    side_effect=network_forbidden,
                ) as http_connect,
                mock.patch.object(
                    socket,
                    "create_connection",
                    side_effect=network_forbidden,
                ) as socket_connect,
            ):
                seed_client = CrossrefClient(
                    cache_dir=cache_dir,
                    mailto=mailto,
                    timeout=1.0,
                    retries=0,
                    request_interval=0.0,
                    use_environment_proxy=False,
                    refresh_cache=False,
                    max_network_requests=1,
                    request_ledger=ledger,
                    request_budget_id=budget_id,
                    require_private_storage=True,
                    budget_registry_dir=registry,
                )
                descriptor = benchmark_builder._crossref_request_descriptor(
                    "/works", request_params, mailto=mailto
                )
                request_url = benchmark_builder._crossref_url_from_descriptor(
                    descriptor
                )
                seed_client._reserve_network_request(request_url)
                seed_client._write_cache_object(
                    seed_client._cache_path(request_url),
                    {"message": {"items": items}},
                )

                args = build_parser().parse_args(
                    [
                        "--data-dir",
                        str(benchmark_builder.DATA_DIR),
                        "--output-dir",
                        str(output),
                        "--cache-dir",
                        str(cache_dir),
                        "--from-date",
                        "2026-01-01",
                        "--until-date",
                        "2026-06-30",
                        "--sample-size",
                        "500",
                        "--samples-per-stratum",
                        "10",
                        "--max-papers-per-journal",
                        "1",
                        "--journal-workers",
                        "1",
                        "--bulk-pages",
                        "1",
                        "--bulk-rows",
                        "500",
                        "--rows-per-journal",
                        "1",
                        "--min-abstract-chars",
                        "300",
                        "--mailto",
                        mailto,
                        "--retries",
                        "0",
                        "--request-interval",
                        "0",
                        "--max-network-requests",
                        "1",
                        "--request-ledger",
                        str(ledger),
                        "--request-budget-id",
                        budget_id,
                        "--request-budget-registry-dir",
                        str(registry),
                        "--require-complete-acquisition-evidence",
                    ]
                )
                with mock.patch.object(
                    benchmark_builder,
                    "load_jcr_venues",
                    return_value=(venues, set()),
                ):
                    manifest = benchmark_builder.build_benchmark(args)
                    persisted = json.loads(
                        (output / "manifest.json").read_text(encoding="utf-8")
                    )
                    dataset_rows = [
                        json.loads(line)
                        for line in (output / "dataset.jsonl")
                        .read_text(encoding="utf-8")
                        .splitlines()
                    ]
                    audit = benchmark_evaluator._validate_formal_acquisition_evidence(
                        persisted,
                        builder_manifest=output / "manifest.json",
                        dataset=output / "dataset.jsonl",
                        dataset_sha256=benchmark_evaluator._file_sha256(
                            output / "dataset.jsonl"
                        ),
                        raw_rows=dataset_rows,
                        expected_count=500,
                        acquisition_window=window,
                        min_abstract_chars=300,
                    )

            opener_open.assert_not_called()
            http_connect.assert_not_called()
            socket_connect.assert_not_called()
            self.assertEqual(manifest["dataset"]["record_count"], 500)
            self.assertTrue(manifest["dataset"]["complete"])
            self.assertEqual(manifest["coverage"]["accepted_records"], 500)
            self.assertEqual(manifest["coverage"]["target_records"], 500)
            self.assertEqual(manifest["coverage"]["complete_strata"], 36)
            self.assertEqual(manifest["source"]["network_requests_this_run"], 0)
            self.assertEqual(manifest["source"]["network_requests_cumulative"], 1)
            self.assertEqual(manifest["source"]["cache_hits"], 1)
            self.assertEqual(manifest["source"]["response_cache_hits"], 1)
            self.assertEqual(len(dataset_rows), 500)
            self.assertEqual(len({row["doi"] for row in dataset_rows}), 500)
            self.assertEqual(
                len({row["gold_journal_id"] for row in dataset_rows}), 500
            )
            self.assertEqual(len({row["gold_entity_id"] for row in dataset_rows}), 500)

            evidence = persisted["acquisition_evidence"]
            self.assertTrue(evidence["complete"])
            self.assertEqual(evidence["dataset_record_count"], 500)
            self.assertEqual(evidence["provenance"]["record_count"], 500)
            self.assertEqual(evidence["cache_leaves"]["record_count"], 1)
            self.assertEqual(evidence["ledger"]["attempt_records"], 1)
            tree = json.loads(
                (output / "cache_evidence_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(tree["accepted_record_count"], 500)
            self.assertEqual(tree["provenance_replay_verified"], 500)
            self.assertEqual(tree["leaf_count"], 1)
            self.assertTrue(tree["complete"])
            provenance = [
                json.loads(line)
                for line in (output / "provenance.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(len(provenance), 500)
            self.assertTrue(
                all(row["ledger_sequences"] == [1] for row in provenance)
            )
            self.assertTrue(
                all(row["observed_via"] == "cache" for row in provenance)
            )
            self.assertEqual(audit["provenance_record_count"], 500)
            self.assertEqual(audit["used_response_count"], 1)
            self.assertEqual(audit["http_attempt_prefix_count"], 1)

    def _make_evidence_fixture(self, root: Path):
        venue = make_venue("known", "0007-9235")
        issn_index = build_issn_index([venue])
        window = BuildWindow(date(2026, 1, 1), date(2026, 4, 30))
        item = make_item()
        raw = json.dumps(
            {"message": {"items": [item]}},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        class FakeOpener:
            def open(self, _request, *, timeout):
                self.timeout = timeout
                return io.BytesIO(raw)

        ledger = root / "request-ledger.jsonl"
        client = CrossrefClient(
            cache_dir=root / "cache",
            mailto="test@example.org",
            timeout=5.0,
            retries=0,
            request_interval=0.0,
            use_environment_proxy=False,
            refresh_cache=False,
            max_network_requests=2,
            request_ledger=ledger,
            request_budget_id="evidence-budget",
            require_private_storage=True,
            budget_registry_dir=root / "budget-registry",
        )
        client.opener = FakeOpener()
        payload, response_evidence = client.get_json_with_evidence(
            "/works",
            {
                "cursor": "*",
                "filter": (
                    "from-pub-date:2026-01-01,until-pub-date:2026-04-30,"
                    "type:journal-article,has-abstract:true"
                ),
                "rows": "1",
            },
        )
        cached_item = payload["message"]["items"][0]
        record, status = prepare_crossref_record(
            cached_item,
            issn_index=issn_index,
            expected_venue=venue,
            window=window,
            min_abstract_chars=100,
        )
        self.assertEqual(status, "ok")
        assert record is not None
        record = _record_with_item_evidence(
            record,
            CrossrefItemEvidence(cached_item, 0, response_evidence),
        )
        return client, ledger, venue, issn_index, window, record

    def test_cache_tamper_is_detected_before_evidence_publication(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            client, ledger, venue, issn_index, window, record = (
                self._make_evidence_fixture(root)
            )
            cache_path = next(client.cache_dir.glob("*.json"))
            tampered = json.loads(cache_path.read_text(encoding="utf-8"))
            tampered["message"]["items"][0]["title"] = ["Tampered title"]
            os.chmod(cache_path, 0o600)
            cache_path.write_text(
                json.dumps(tampered, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "cache hash/size mismatch"):
                _verify_acquisition_evidence(
                    [record],
                    cache_dir=client.cache_dir,
                    venues=[venue],
                    issn_index=issn_index,
                    window=window,
                    min_abstract_chars=100,
                    request_ledger=ledger,
                    request_budget_id="evidence-budget",
                    hard_http_attempt_ceiling=2,
                    budget_binding_path=client.budget_binding_path,
                    budget_registry_claim_path=client.budget_registry_claim_path,
                    request_highwater_path=client.request_highwater_path,
                    global_usage_path=client.global_usage_path,
                    mailto="test@example.org",
                    bulk_rows=1,
                    rows_per_journal=1,
                    require_complete=True,
                )

    def test_request_descriptor_cannot_claim_a_non_crossref_origin(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            client, ledger, venue, issn_index, window, record = (
                self._make_evidence_fixture(root)
            )
            record["_crossref_acquisition_evidence"]["request_descriptor"][
                "base_url"
            ] = "https://example.invalid"
            with self.assertRaisesRegex(ValueError, "fixed official protocol"):
                _verify_acquisition_evidence(
                    [record],
                    cache_dir=client.cache_dir,
                    venues=[venue],
                    issn_index=issn_index,
                    window=window,
                    min_abstract_chars=100,
                    request_ledger=ledger,
                    request_budget_id="evidence-budget",
                    hard_http_attempt_ceiling=2,
                    budget_binding_path=client.budget_binding_path,
                    budget_registry_claim_path=client.budget_registry_claim_path,
                    request_highwater_path=client.request_highwater_path,
                    global_usage_path=client.global_usage_path,
                    mailto="test@example.org",
                    bulk_rows=1,
                    rows_per_journal=1,
                    require_complete=True,
                )

    def test_replay_bundle_binds_dataset_cache_ledger_and_builder_hashes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            client, ledger, venue, issn_index, window, record = (
                self._make_evidence_fixture(root)
            )
            records, provenance, leaves, tree = _verify_acquisition_evidence(
                [record],
                cache_dir=client.cache_dir,
                venues=[venue],
                issn_index=issn_index,
                window=window,
                min_abstract_chars=100,
                request_ledger=ledger,
                request_budget_id="evidence-budget",
                hard_http_attempt_ceiling=2,
                budget_binding_path=client.budget_binding_path,
                budget_registry_claim_path=client.budget_registry_claim_path,
                request_highwater_path=client.request_highwater_path,
                global_usage_path=client.global_usage_path,
                mailto="test@example.org",
                bulk_rows=1,
                rows_per_journal=1,
                require_complete=True,
            )
            self.assertTrue(tree["complete"])
            self.assertEqual(provenance[0]["ledger_sequences"], [1])
            self.assertEqual(leaves[0]["ledger_sequences"], [1])
            output = root / "final"
            manifest = {
                "schema_version": 1,
                "builder": "scripts/build_recent_journal_benchmark.py",
                "dataset": {
                    "path": "dataset.jsonl",
                    "record_count": 1,
                    "sha256": "pending",
                    "complete": True,
                },
            }
            _finalize_benchmark_outputs(
                output,
                records,
                manifest,
                allow_incomplete=False,
                provenance_rows=provenance,
                cache_evidence_leaves=leaves,
                cache_evidence_tree=tree,
            )
            persisted = json.loads((output / "manifest.json").read_text())
            evidence = persisted["acquisition_evidence"]
            self.assertTrue(evidence["complete"])
            self.assertEqual(evidence["ledger"]["hard_http_attempt_ceiling"], 2)
            self.assertEqual(
                evidence["ledger"]["budget_binding"]["artifact_type"],
                "crossref_http_attempt_budget_binding",
            )
            self.assertEqual(
                evidence["ledger"]["global_budget_claim"]["artifact_type"],
                "crossref_global_http_attempt_budget_claim",
            )
            self.assertFalse(Path(evidence["ledger"]["path"]).is_absolute())
            self.assertIn("not cryptographic attestation", evidence["assurance_scope"])
            self.assertEqual(
                leaves[0]["request_descriptor"]["base_url"],
                "https://api.crossref.org",
            )
            self.assertTrue(
                provenance[0]["cache_relative_path"].startswith("raw_cache/")
            )
            self.assertEqual(
                evidence["provenance"]["sha256"],
                _sha256_file(output / "provenance.jsonl"),
            )
            self.assertEqual(
                evidence["cache_leaves"]["sha256"],
                _sha256_file(output / "cache_evidence.jsonl"),
            )
            self.assertEqual(
                evidence["cache_tree"]["sha256"],
                _sha256_file(output / "cache_evidence_manifest.json"),
            )
            self.assertEqual(
                evidence["ledger"]["sha256"],
                _sha256_file(output / "request-ledger-prefix.jsonl"),
            )
            self.assertEqual(
                evidence["ledger"]["highwater"]["sha256"],
                _sha256_file(output / "request-ledger-highwater-prefix.jsonl"),
            )
            self.assertEqual(
                evidence["ledger"]["global_usage"]["sha256"],
                _sha256_file(output / "request-ledger-global-prefix.jsonl"),
            )
            self.assertEqual(evidence["ledger"]["sha256"], _sha256_file(ledger))
            self.assertEqual(
                (output / "request-ledger-prefix.jsonl").read_bytes(),
                (output / "request-ledger-highwater-prefix.jsonl").read_bytes(),
            )
            self.assertEqual(
                (output / "request-ledger-prefix.jsonl").read_bytes(),
                (output / "request-ledger-global-prefix.jsonl").read_bytes(),
            )
            self.assertEqual(
                evidence["builder_source"]["sha256"],
                _sha256_file(Path("scripts/build_recent_journal_benchmark.py")),
            )
            for name in (
                "dataset.jsonl",
                "provenance.jsonl",
                "cache_evidence.jsonl",
                "cache_evidence_manifest.json",
                "manifest.json",
            ):
                self.assertEqual(
                    stat.S_IMODE((output / name).stat().st_mode), 0o444
                )
            self.assertEqual(
                stat.S_IMODE(next(client.cache_dir.glob("*.json")).stat().st_mode),
                0o400,
            )
            self.assertEqual(stat.S_IMODE(ledger.stat().st_mode), 0o600)
            raw_snapshot = output / leaves[0]["cache_relative_path"]
            self.assertTrue(raw_snapshot.is_file())
            self.assertEqual(stat.S_IMODE(raw_snapshot.stat().st_mode), 0o400)
            for name in (
                "request-ledger-prefix.jsonl",
                "request-ledger-highwater-prefix.jsonl",
                "request-ledger-global-prefix.jsonl",
                "request-budget-binding.json",
                "request-budget-global-claim.json",
            ):
                self.assertEqual(stat.S_IMODE((output / name).stat().st_mode), 0o444)
            self.assertEqual(stat.S_IMODE((output / "raw_cache").stat().st_mode), 0o700)
            tree_text = (output / "cache_evidence_manifest.json").read_text()
            self.assertNotIn(str(root), tree_text)
            snapshot_sha = _sha256_file(output / "request-ledger-prefix.jsonl")
            with ledger.open("a", encoding="utf-8") as handle:
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            self.assertEqual(
                _sha256_file(output / "request-ledger-prefix.jsonl"), snapshot_sha
            )

    def test_ledger_tamper_between_replay_and_publish_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            client, ledger, venue, issn_index, window, record = (
                self._make_evidence_fixture(root)
            )
            records, provenance, leaves, tree = _verify_acquisition_evidence(
                [record],
                cache_dir=client.cache_dir,
                venues=[venue],
                issn_index=issn_index,
                window=window,
                min_abstract_chars=100,
                request_ledger=ledger,
                request_budget_id="evidence-budget",
                hard_http_attempt_ceiling=2,
                budget_binding_path=client.budget_binding_path,
                budget_registry_claim_path=client.budget_registry_claim_path,
                request_highwater_path=client.request_highwater_path,
                global_usage_path=client.global_usage_path,
                mailto="test@example.org",
                bulk_rows=1,
                rows_per_journal=1,
                require_complete=True,
            )
            with ledger.open("a", encoding="utf-8") as handle:
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            with self.assertRaisesRegex(ValueError, "ledger changed"):
                _finalize_benchmark_outputs(
                    root / "final",
                    records,
                    {
                        "dataset": {
                            "path": "dataset.jsonl",
                            "record_count": 1,
                            "sha256": "pending",
                            "complete": True,
                        }
                    },
                    allow_incomplete=False,
                    provenance_rows=provenance,
                    cache_evidence_leaves=leaves,
                    cache_evidence_tree=tree,
                )
            self.assertFalse((root / "final").exists())


if __name__ == "__main__":
    unittest.main()
