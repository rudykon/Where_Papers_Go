from __future__ import annotations

import math
import os
from pathlib import Path
import tempfile
import unittest

from research.model_runs import (
    TITLE_ABSTRACT_SEPARATOR,
    LocalScientificEncoderProvider,
)
from research.reranker_runs import LocalBGECrossEncoderProvider


os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
try:
    import torch
except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
    if exc.name != "torch":
        raise
    _MODEL_RUNTIME_AVAILABLE = False
    _MODEL_RUNTIME_ERROR = f"{type(exc).__name__}: {exc}"
else:  # A present but broken runtime must fail discovery, not silently skip.
    from transformers import (
        BertConfig,
        BertForSequenceClassification,
        BertModel,
        BertTokenizer,
    )

    _MODEL_RUNTIME_AVAILABLE = True
    _MODEL_RUNTIME_ERROR = ""


@unittest.skipUnless(
    _MODEL_RUNTIME_AVAILABLE,
    f"optional torch/transformers runtime unavailable: {_MODEL_RUNTIME_ERROR}",
)
class LocalModelRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        vocabulary = self.root / "vocab.txt"
        vocabulary.write_text(
            "\n".join(
                (
                    "[PAD]",
                    "[UNK]",
                    "[CLS]",
                    "[SEP]",
                    "[MASK]",
                    "graph",
                    "retrieval",
                    "cancer",
                    "imaging",
                    "evidence",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        self.tokenizer = BertTokenizer(
            vocab_file=str(vocabulary), do_lower_case=True
        )
        torch.manual_seed(20260828)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _configuration(self, *, num_labels: int | None = None) -> BertConfig:
        values = {
            "vocab_size": 10,
            "hidden_size": 16,
            "num_hidden_layers": 1,
            "num_attention_heads": 2,
            "intermediate_size": 32,
        }
        if num_labels is not None:
            values["num_labels"] = num_labels
        return BertConfig(**values)

    def test_scientific_cls_provider_loads_local_safetensors(self) -> None:
        model_dir = self.root / "scientific"
        BertModel(self._configuration()).save_pretrained(
            model_dir, safe_serialization=True
        )
        self.tokenizer.save_pretrained(model_dir)
        provider = LocalScientificEncoderProvider(
            protocol="scincl",
            model_dir=model_dir,
            model_repo="unit/scientific",
            model_revision="a" * 40,
            device="cpu",
            batch_size=2,
            fp16=False,
        )
        vectors = provider.embed(
            (
                "graph retrieval"
                + TITLE_ABSTRACT_SEPARATOR
                + "graph evidence",
                "cancer imaging"
                + TITLE_ABSTRACT_SEPARATOR
                + "cancer evidence",
            )
        )
        self.assertEqual((len(vectors), len(vectors[0])), (2, 16))
        for vector in vectors:
            self.assertTrue(all(math.isfinite(value) for value in vector))
            self.assertAlmostEqual(sum(value * value for value in vector), 1.0, places=5)

    def test_cross_encoder_provider_loads_local_safetensors(self) -> None:
        model_dir = self.root / "reranker"
        BertForSequenceClassification(
            self._configuration(num_labels=1)
        ).save_pretrained(model_dir, safe_serialization=True)
        self.tokenizer.save_pretrained(model_dir)
        provider = LocalBGECrossEncoderProvider(
            model_dir=model_dir,
            model_repo="unit/reranker",
            model_revision="b" * 40,
            device="cpu",
            batch_size=2,
            fp16=False,
        )
        scores = provider.score_pairs(
            (
                ("graph retrieval", "graph evidence"),
                ("cancer imaging", "cancer evidence"),
            )
        )
        self.assertEqual(len(scores), 2)
        self.assertTrue(all(math.isfinite(score) for score in scores))


if __name__ == "__main__":
    unittest.main()
