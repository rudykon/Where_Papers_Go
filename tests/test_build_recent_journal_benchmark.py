from __future__ import annotations

import io
import http.client
import json
import unittest
import urllib.error
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.build_recent_journal_benchmark import (
    BuildWindow,
    CrossrefClient,
    JournalVenue,
    allocate_stratum_targets,
    _finalize_benchmark_outputs,
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
            self.assertFalse(output.exists())


class CrossrefRequestSafetyTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
