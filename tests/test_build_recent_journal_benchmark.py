from __future__ import annotations

import unittest
from datetime import date

from scripts.build_recent_journal_benchmark import (
    BuildWindow,
    JournalVenue,
    allocate_stratum_targets,
    build_issn_index,
    classify_broad_field,
    jats_to_text,
    prepare_crossref_record,
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


if __name__ == "__main__":
    unittest.main()
