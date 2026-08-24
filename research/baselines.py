"""Standard-library lexical baselines with a shared offline run interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter, defaultdict
import math
import re
import unicodedata
from typing import Iterable, Mapping, Sequence

from .types import Query, Run, VenueDocument, sort_ranking


_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+#.\-]*|[\u3400-\u9fff]+", re.I)
_ENGLISH_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "is", "it", "of", "on", "or", "our", "that", "the", "this",
    "to", "using", "we", "with",
}


def tokenize(text: str) -> list[str]:
    """Tokenize English and Chinese deterministically without external models.

    Chinese chunks contribute unigrams and bigrams.  The lexical baselines are
    intentionally simple and transparent; neural systems should be imported
    as frozen score runs through :func:`research.data.load_score_run`.
    """

    normalized = unicodedata.normalize("NFKC", text).casefold().replace("&", " and ")
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(normalized):
        value = match.group(0).strip(".-")
        if not value:
            continue
        if re.fullmatch(r"[\u3400-\u9fff]+", value):
            tokens.extend(value)
            tokens.extend(value[index : index + 2] for index in range(len(value) - 1))
        else:
            if value not in _ENGLISH_STOPWORDS:
                tokens.append(value)
    return tokens


class Baseline(ABC):
    """Every retriever emits the same ``query -> ranked documents`` run."""

    name: str

    @abstractmethod
    def fit(self, corpus: Sequence[VenueDocument]) -> "Baseline":
        raise NotImplementedError

    @abstractmethod
    def run(self, queries: Iterable[Query], *, top_k: int) -> Run:
        raise NotImplementedError


def _index_units(
    corpus: Sequence[VenueDocument], *, use_prototypes: bool
) -> list[tuple[str, str]]:
    """Expand one venue into independently scored, temporally eligible units."""

    units: list[tuple[str, str]] = []
    for document in corpus:
        raw = document.metadata.get("prototypes") if use_prototypes else None
        prototypes = raw if isinstance(raw, list) else []
        texts = [
            str(prototype.get("text") or "").strip()
            for prototype in prototypes
            if isinstance(prototype, Mapping)
            and prototype.get("temporal_eligible", True) is not False
            and str(prototype.get("text") or "").strip()
        ]
        if not texts:
            texts = [document.text]
        units.extend((document.doc_id, text) for text in texts)
    return units


class BM25Baseline(Baseline):
    def __init__(
        self,
        *,
        name: str = "bm25",
        k1: float = 1.2,
        b: float = 0.75,
        use_prototypes: bool = False,
    ) -> None:
        if k1 <= 0 or not 0 <= b <= 1:
            raise ValueError("BM25 requires k1 > 0 and 0 <= b <= 1")
        self.name = name
        self.k1 = float(k1)
        self.b = float(b)
        self.use_prototypes = bool(use_prototypes)
        self._documents: tuple[VenueDocument, ...] = ()
        self._unit_doc_ids: tuple[str, ...] = ()
        self._postings: dict[str, list[tuple[int, int]]] = {}
        self._lengths: tuple[int, ...] = ()
        self._average_length = 0.0
        self._idf: dict[str, float] = {}

    def fit(self, corpus: Sequence[VenueDocument]) -> "BM25Baseline":
        self._documents = tuple(corpus)
        units = _index_units(self._documents, use_prototypes=self.use_prototypes)
        self._unit_doc_ids = tuple(doc_id for doc_id, _text in units)
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        lengths: list[int] = []
        for index, (_doc_id, text) in enumerate(units):
            counts = Counter(tokenize(text))
            lengths.append(sum(counts.values()))
            for term, frequency in counts.items():
                postings[term].append((index, frequency))
        count = len(units)
        self._postings = dict(postings)
        self._lengths = tuple(lengths)
        self._average_length = sum(lengths) / count if count else 0.0
        self._idf = {
            term: math.log(1.0 + (count - len(items) + 0.5) / (len(items) + 0.5))
            for term, items in self._postings.items()
        }
        return self

    def run(self, queries: Iterable[Query], *, top_k: int) -> Run:
        if not self._documents:
            raise RuntimeError("BM25 baseline must be fitted before run()")
        output: Run = {}
        average_length = self._average_length or 1.0
        for query in queries:
            unit_scores: dict[int, float] = defaultdict(float)
            query_counts = Counter(tokenize(query.text))
            for term, query_frequency in query_counts.items():
                for document_index, term_frequency in self._postings.get(term, ()):
                    length = self._lengths[document_index]
                    denominator = term_frequency + self.k1 * (
                        1.0 - self.b + self.b * length / average_length
                    )
                    score = self._idf[term] * (
                        term_frequency * (self.k1 + 1.0) / denominator
                    )
                    unit_scores[document_index] += query_frequency * score
            scores: dict[str, float] = {}
            for document_index, score in unit_scores.items():
                doc_id = self._unit_doc_ids[document_index]
                scores[doc_id] = max(scores.get(doc_id, float("-inf")), score)
            output[query.query_id] = sort_ranking(scores, top_k)
        return output


class TfidfBaseline(Baseline):
    """Sparse TF-IDF cosine retrieval using the same frozen corpus."""

    def __init__(
        self,
        *,
        name: str = "tfidf",
        sublinear_tf: bool = True,
        use_prototypes: bool = False,
    ) -> None:
        self.name = name
        self.sublinear_tf = sublinear_tf
        self.use_prototypes = bool(use_prototypes)
        self._documents: tuple[VenueDocument, ...] = ()
        self._unit_doc_ids: tuple[str, ...] = ()
        self._idf: dict[str, float] = {}
        self._postings: dict[str, list[tuple[int, float]]] = {}

    def _tf(self, frequency: int) -> float:
        return 1.0 + math.log(frequency) if self.sublinear_tf else float(frequency)

    def fit(self, corpus: Sequence[VenueDocument]) -> "TfidfBaseline":
        self._documents = tuple(corpus)
        units = _index_units(self._documents, use_prototypes=self.use_prototypes)
        self._unit_doc_ids = tuple(doc_id for doc_id, _text in units)
        document_counts = [Counter(tokenize(text)) for _doc_id, text in units]
        frequencies: Counter[str] = Counter()
        for counts in document_counts:
            frequencies.update(counts.keys())
        # Document frequency is measured over independently scored prototype
        # units, so the IDF population must use that same unit definition.
        # Using the venue count here makes df > N whenever one term appears in
        # prototypes from the same venue and distorts every cosine weight.
        count = len(units)
        self._idf = {
            term: math.log((1.0 + count) / (1.0 + frequency)) + 1.0
            for term, frequency in frequencies.items()
        }
        postings: dict[str, list[tuple[int, float]]] = defaultdict(list)
        for index, counts in enumerate(document_counts):
            weights = {
                term: self._tf(frequency) * self._idf[term]
                for term, frequency in counts.items()
            }
            norm = math.sqrt(sum(weight * weight for weight in weights.values())) or 1.0
            for term, weight in weights.items():
                postings[term].append((index, weight / norm))
        self._postings = dict(postings)
        return self

    def run(self, queries: Iterable[Query], *, top_k: int) -> Run:
        if not self._documents:
            raise RuntimeError("TF-IDF baseline must be fitted before run()")
        output: Run = {}
        for query in queries:
            counts = Counter(tokenize(query.text))
            query_weights = {
                term: self._tf(frequency) * self._idf[term]
                for term, frequency in counts.items()
                if term in self._idf
            }
            query_norm = math.sqrt(sum(value * value for value in query_weights.values()))
            unit_scores: dict[int, float] = defaultdict(float)
            if query_norm:
                for term, weight in query_weights.items():
                    normalized_weight = weight / query_norm
                    for document_index, document_weight in self._postings.get(term, ()):
                        unit_scores[document_index] += (
                            normalized_weight * document_weight
                        )
            scores: dict[str, float] = {}
            for document_index, score in unit_scores.items():
                doc_id = self._unit_doc_ids[document_index]
                scores[doc_id] = max(scores.get(doc_id, float("-inf")), score)
            output[query.query_id] = sort_ranking(scores, top_k)
        return output


class ImportedRunBaseline(Baseline):
    """Adapter that exposes frozen vector or graph scores as a baseline."""

    def __init__(self, run: Run, *, name: str = "imported") -> None:
        self.name = name
        self._run = run

    def fit(self, corpus: Sequence[VenueDocument]) -> "ImportedRunBaseline":
        del corpus
        return self

    def run(self, queries: Iterable[Query], *, top_k: int) -> Run:
        return {
            query.query_id: list(self._run.get(query.query_id, ()))[:top_k]
            for query in queries
        }
