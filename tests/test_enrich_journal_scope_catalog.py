from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.enrich_journal_scope_catalog import (
    ScopeEntity,
    append_attempt,
    load_attempted_entity_ids,
    prioritized_entities,
)


def entity(
    entity_id: int,
    *,
    field: str,
    quartile: str,
    issn: str,
    automatic_ok: bool = False,
    status: str = "",
) -> ScopeEntity:
    return ScopeEntity(
        entity_id=entity_id,
        name=f"Journal {entity_id}",
        issns=(issn,),
        quartile=quartile,
        category="TEST",
        broad_field=field,
        row_ids=(entity_id,),
        automatic_scope_ok=automatic_ok,
        automatic_status=status,
        reviewed_scope_available=False,
    )


class ScopeQueueTests(unittest.TestCase):
    def test_benchmark_entities_are_first_and_completed_entities_are_skipped(self) -> None:
        values = [
            entity(1, field="medicine", quartile="Q1", issn="11111111"),
            entity(2, field="medicine", quartile="Q2", issn="22222222"),
            entity(3, field="history", quartile="Q1", issn="33333333"),
            entity(
                4,
                field="history",
                quartile="Q2",
                issn="44444444",
                automatic_ok=True,
            ),
        ]
        queue = prioritized_entities(
            values,
            seed="fixed",
            priority_issns={"33333333"},
        )
        self.assertEqual(queue[0].entity_id, 3)
        self.assertEqual({item.entity_id for item in queue}, {1, 2, 3})

    def test_failed_priority_entity_follows_unattempted_priority_entity(self) -> None:
        queue = prioritized_entities(
            [
                entity(
                    1,
                    field="a",
                    quartile="Q1",
                    issn="11111111",
                    status="not_relevant",
                ),
                entity(2, field="b", quartile="Q2", issn="22222222"),
            ],
            seed="fixed",
            priority_issns={"11111111", "22222222"},
            attempted_entity_ids={1},
        )
        self.assertEqual([item.entity_id for item in queue], [2, 1])

    def test_automatic_queue_can_skip_every_attempted_entity(self) -> None:
        queue = prioritized_entities(
            [
                entity(1, field="a", quartile="Q1", issn="11111111"),
                entity(2, field="b", quartile="Q2", issn="22222222"),
            ],
            seed="fixed",
            attempted_entity_ids={1},
            retry_attempted=False,
        )
        self.assertEqual([item.entity_id for item in queue], [2])

    def test_requeue_event_makes_entity_eligible_again(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "attempts.jsonl"
            append_attempt(path, {"entity_id": 7, "status": "no_candidate_pages"})
            self.assertEqual(load_attempted_entity_ids(path), {7})
            append_attempt(path, {"entity_id": 7, "event": "requeue"})
            self.assertEqual(load_attempted_entity_ids(path), set())

    def test_nonpriority_queue_interleaves_field_and_quartile_buckets(self) -> None:
        values = [
            entity(1, field="a", quartile="Q1", issn="11111111"),
            entity(2, field="a", quartile="Q1", issn="22222222"),
            entity(3, field="b", quartile="Q2", issn="33333333"),
            entity(4, field="b", quartile="Q2", issn="44444444"),
        ]
        queue = prioritized_entities(values, seed="fixed")
        self.assertNotEqual(queue[0].broad_field, queue[1].broad_field)
        self.assertNotEqual(queue[2].broad_field, queue[3].broad_field)


if __name__ == "__main__":
    unittest.main()
