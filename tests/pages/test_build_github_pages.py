"""Pages export tests, intentionally separate from the fixed backend test suite."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import build_github_pages as pages


class PublicCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = pages.build_catalog()

    def test_catalog_preserves_real_sources_and_merges_ranking_entities(self):
        counts = {source["id"]: source["records"] for source in self.catalog["sources"]}
        self.assertEqual(counts, {"ccf": 386, "th_cpl": 406, "cas": 21772, "jcr": 22643})
        venues = self.catalog["venues"]
        self.assertEqual(len({venue["id"] for venue in venues}), len(venues))
        self.assertLess(len(venues), sum(counts.values()))
        neurips = [venue for venue in venues if venue["abbreviation"] == "NeurIPS"]
        self.assertEqual(len(neurips), 1)
        self.assertIn("CCF-A", neurips[0]["ranks"])
        self.assertIn("THCPL-A", neurips[0]["ranks"])
        self.assertTrue(neurips[0]["reviewed"])

    def test_only_reviewed_scopes_and_explicit_public_fields_are_exported(self):
        approved = {
            scope.scope_id: scope
            for scope in pages.recommender.load_curated_scopes().values()
        }
        source_texts = {scope.scope_summary for scope in approved.values()}
        urls = {pages.public_url(scope.source_url) for scope in approved.values()}
        for venue in self.catalog["venues"]:
            self.assertEqual(set(venue), pages.PUBLIC_VENUE_FIELDS)
            if venue["reviewed"]:
                self.assertIn(venue["scope"], source_texts)
                self.assertIn(venue["scope_url"], urls)
            else:
                self.assertEqual(venue["scope"], "")
                self.assertEqual(venue["scope_url"], "")
                self.assertEqual(venue["keywords"], [])
        encoded = json.dumps(self.catalog, ensure_ascii=False)
        for private_field in (
            "source_file",
            "official_scope",
            "reviewed_by",
            "review_notes",
            "llmapi.json",
            str(pages.ROOT),
        ):
            self.assertNotIn(private_field, encoded)

    def test_inactive_reviewed_targets_are_excluded(self):
        records = pages.recommender.load_records()
        inactive_ids = {
            f"venue:{min(record.row_id for record in group)}"
            for group in pages.recommender.group_records(records)
            if any(
                record.curated_target_status in {"historical_merged", "family_non_actionable"}
                for record in group
            )
        }
        self.assertTrue(inactive_ids)
        self.assertFalse(inactive_ids & {venue["id"] for venue in self.catalog["venues"]})
        self.assertEqual(self.catalog["stats"]["venues"], len(self.catalog["venues"]))
        self.assertEqual(
            self.catalog["stats"]["reviewed"],
            sum(venue["reviewed"] for venue in self.catalog["venues"]),
        )

    def test_public_url_rejects_non_web_links_and_private_hosts(self):
        for value in (
            "/home/user/private",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "https://user:secret@example.org",
            "http://localhost:8080",
            "http://127.0.0.1",
            "https://[::1]",
            "https://internal.local",
            "https://example.org:bad",
            "https://example.org\nsecret",
        ):
            with self.subTest(value=value):
                self.assertEqual(pages.public_url(value), "")
        self.assertEqual(pages.public_url("https://example.org/cfp"), "https://example.org/cfp")

    def test_browser_normalized_private_ip_spellings_are_rejected(self):
        for value in (
            "http://127.1",
            "http://127.0.1",
            "http://0177.0.0.1",
            "http://2130706433",
            "http://0x7f000001",
            "http://0x7f.0.0.1",
            "http://127.0.0.0x1",
            "http://0.0.0.0",
            "http://127。0。0。1",
            "http://127.0.0.1。",
            "http://１２７.０.０.１",
            "http://%31%32%37.%30.%30.%31",
            "http://localhost。",
        ):
            with self.subTest(value=value):
                self.assertEqual(pages.public_url(value), "")
        for value in ("https://8.8.8.8/cfp", "https://example.org/127.1", "https://例子.测试/"):
            with self.subTest(value=value):
                self.assertEqual(pages.public_url(value), value)


class StaticArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.website = self.root / "website"
        self.output = self.root / "output"
        for name in pages.STATIC_FILES:
            path = self.website / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("public asset", encoding="utf-8")
        self.catalog = {
            "schema_version": 1,
            "sources": [],
            "venues": [],
            "stats": {"venues": 0, "reviewed": 0},
        }

    def test_artifact_copies_only_explicit_assets(self):
        (self.website / "llmapi.json").write_text('{"secret": "must stay private"}')
        (self.website / "assets" / "cache.json").write_text('{"query": "private"}')
        (self.website / "assets" / "api-client.js").write_text("legacy API client")
        with patch.object(pages, "build_catalog", return_value=self.catalog):
            pages.build_site(self.output, website_dir=self.website)
            pages.build_site(self.output, website_dir=self.website)
        files = {
            path.relative_to(self.output).as_posix()
            for path in self.output.rglob("*")
            if path.is_file()
        }
        self.assertEqual(files, set(pages.STATIC_FILES + pages.GENERATED_FILES))
        self.assertIn("assets/search-worker.js", files)
        self.assertIn("assets/search-engine.js", files)
        self.assertNotIn("assets/api-client.js", files)
        self.assertEqual(
            json.loads((self.output / "assets/catalog.json").read_text()), self.catalog
        )

    def test_unknown_output_files_are_not_overwritten_or_published(self):
        self.output.mkdir()
        (self.output / "private.json").write_text("keep me")
        with self.assertRaisesRegex(ValueError, "Unexpected existing"):
            pages.build_site(self.output, website_dir=self.website)
        self.assertEqual((self.output / "private.json").read_text(), "keep me")

    def test_symlinked_assets_are_rejected(self):
        target = self.root / "secret.css"
        target.write_text("private")
        stylesheet = self.website / "assets/styles.css"
        stylesheet.unlink()
        stylesheet.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            pages.build_site(self.output, website_dir=self.website)


if __name__ == "__main__":
    unittest.main()
