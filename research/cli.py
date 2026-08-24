"""Command-line entry point for the frozen offline research benchmark."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import threading
from typing import Any, Mapping, Sequence

from .baselines import BM25Baseline, ImportedRunBaseline, TfidfBaseline
from .cache_builder import build_cached_corpus
from .data import (
    ResearchDataError,
    build_data_manifest,
    load_jcr_corpus,
    load_jsonl_corpus,
    load_recent_journal_dataset,
    load_score_run,
    temporal_split,
    write_run,
)
from .fusion import LearnedLinearFusion, rrf_fuse
from .historical_builder import (
    CachedJsonClient,
    CollectionPolicy,
    CrossrefHistoricalSource,
    HistoricalCollectionError,
    OfficialScopeSearchSource,
    OpenAlexHistoricalSource,
    PCLPrototypeClient,
    load_venue_seeds,
    run_historical_collection,
)
from .leakage import audit_leakage, identity_unsafe_query_ids
from .metrics import evaluate_run, stratified_metrics
from .pcl_retry import PCLRetryPolicy
from .prototype_vectors import build_prototype_vector_run, pcl_embedding_provider
from .reporting import STRATIFICATION_POLICY, build_query_strata, summarize_strata
from .statistics import paired_bootstrap_ci, paired_permutation_test
from .types import Run


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchDataError(f"configuration field {name!r} must be an object")
    return value


def _resolve(config_path: Path, value: Any) -> Path:
    path = Path(str(value or ""))
    if not str(path):
        raise ResearchDataError("configuration contains an empty path")
    return path if path.is_absolute() else (config_path.parent / path).resolve()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _validate_imported_candidates(run: Run, candidate_ids: set[str], name: str) -> None:
    unknown = {
        item.doc_id
        for ranking in run.values()
        for item in ranking
        if item.doc_id not in candidate_ids
    }
    if unknown:
        examples = ", ".join(sorted(unknown)[:5])
        raise ResearchDataError(
            f"imported run {name!r} contains {len(unknown)} IDs outside the frozen corpus: {examples}"
        )


def evaluate_config(config_path: Path) -> dict[str, Any]:
    config_path = config_path.resolve()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchDataError(f"cannot read configuration: {config_path}") from exc
    config = _mapping(config, "root")
    if config.get("offline_only") is not True:
        raise ResearchDataError("research evaluation requires offline_only=true")

    dataset_config = _mapping(config.get("dataset"), "dataset")
    corpus_config = _mapping(config.get("corpus"), "corpus")
    split_config = _mapping(config.get("temporal_split"), "temporal_split")
    evaluation_config = _mapping(config.get("evaluation", {}), "evaluation")
    dataset_path = _resolve(config_path, dataset_config.get("path"))
    corpus_path = _resolve(config_path, corpus_config.get("path"))
    output_dir = _resolve(config_path, config.get("output_dir", "outputs/offline"))

    bundle = load_recent_journal_dataset(
        dataset_path,
        query_fields=tuple(dataset_config.get("query_fields") or ("title", "abstract")),
        relevance_field=str(dataset_config.get("relevance_field") or "gold_journal_id"),
    )
    split = temporal_split(
        bundle.queries,
        train_end=str(split_config.get("train_end") or ""),
        validation_end=str(split_config.get("validation_end") or split_config.get("dev_end") or ""),
        test_end=str(split_config.get("test_end") or ""),
        start=str(split_config["start"]) if split_config.get("start") else None,
    )

    corpus_type = str(corpus_config.get("type") or "jsonl")
    if corpus_type == "jcr_csv":
        corpus = load_jcr_corpus(
            corpus_path,
            snapshot_date=str(corpus_config.get("snapshot_date") or ""),
            text_fields=tuple(corpus_config.get("text_fields") or ("name", "收稿方向", "area")),
            allowed_levels=tuple(corpus_config.get("allowed_levels") or ("Q1", "Q2", "Q3", "Q4")),
        )
    elif corpus_type == "jsonl":
        corpus = load_jsonl_corpus(
            corpus_path,
            id_field=str(corpus_config.get("id_field") or "venue_id"),
            text_fields=tuple(corpus_config.get("text_fields") or ("name", "scope")),
            snapshot_field=str(corpus_config.get("snapshot_field") or "snapshot_date"),
            default_snapshot_date=str(corpus_config.get("snapshot_date") or ""),
        )
    else:
        raise ResearchDataError(f"unsupported corpus type: {corpus_type!r}")

    leakage = audit_leakage(bundle, corpus, split)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "leakage_audit.json", leakage)
    if config.get("fail_on_critical_leakage", True) and not leakage["passed"]:
        raise ResearchDataError(
            f"critical leakage found; inspect {output_dir / 'leakage_audit.json'}"
        )

    query_ids = set((*split.train, *split.validation, *split.test))
    queries = [query for query in bundle.queries if query.query_id in query_ids]
    retrieval_depth = int(evaluation_config.get("retrieval_depth") or 100)
    cutoffs = tuple(int(value) for value in evaluation_config.get("cutoffs", (1, 3, 5, 10, 20, 50)))
    if retrieval_depth < max(cutoffs):
        raise ResearchDataError("retrieval_depth must be at least the largest evaluation cutoff")

    runs: dict[str, Run] = {}
    method_metadata: dict[str, Any] = {}
    for baseline_config in config.get("baselines", ()):
        baseline_config = _mapping(baseline_config, "baselines[]")
        kind = str(baseline_config.get("type") or "")
        name = str(baseline_config.get("name") or kind)
        if not name or name in runs:
            raise ResearchDataError(f"duplicate or empty baseline name: {name!r}")
        if kind == "bm25":
            baseline = BM25Baseline(
                name=name,
                k1=float(baseline_config.get("k1", 1.2)),
                b=float(baseline_config.get("b", 0.75)),
                use_prototypes=bool(baseline_config.get("use_prototypes", False)),
            )
        elif kind == "tfidf":
            baseline = TfidfBaseline(
                name=name,
                sublinear_tf=bool(baseline_config.get("sublinear_tf", True)),
                use_prototypes=bool(baseline_config.get("use_prototypes", False)),
            )
        else:
            raise ResearchDataError(f"unsupported baseline type: {kind!r}")
        runs[name] = baseline.fit(corpus).run(queries, top_k=retrieval_depth)
        method_metadata[name] = dict(baseline_config)

    additional_inputs: list[Path] = []
    for imported_config in config.get("imported_runs", ()):
        imported_config = _mapping(imported_config, "imported_runs[]")
        name = str(imported_config.get("name") or "").strip()
        if not name or name in runs:
            raise ResearchDataError(f"duplicate or empty imported run name: {name!r}")
        path = _resolve(config_path, imported_config.get("path"))
        imported = load_score_run(path, top_k=retrieval_depth)
        _validate_imported_candidates(imported, {doc.doc_id for doc in corpus}, name)
        adapter = ImportedRunBaseline(imported, name=name)
        runs[name] = adapter.fit(corpus).run(queries, top_k=retrieval_depth)
        additional_inputs.append(path)
        method_metadata[name] = dict(imported_config)

    for fusion_config in config.get("fusions", ()):
        fusion_config = _mapping(fusion_config, "fusions[]")
        kind = str(fusion_config.get("type") or "")
        name = str(fusion_config.get("name") or kind)
        source_names = tuple(str(value) for value in fusion_config.get("sources", ()))
        if not name or name in runs:
            raise ResearchDataError(f"duplicate or empty fusion name: {name!r}")
        missing = [source for source in source_names if source not in runs]
        if not source_names or missing:
            raise ResearchDataError(f"fusion {name!r} has missing sources: {missing}")
        source_runs = {source: runs[source] for source in source_names}
        if kind == "rrf":
            runs[name] = rrf_fuse(
                source_runs,
                top_k=retrieval_depth,
                rrf_k=int(fusion_config.get("rrf_k", 60)),
                weights=fusion_config.get("weights") if isinstance(fusion_config.get("weights"), Mapping) else None,
            )
            method_metadata[name] = dict(fusion_config)
        elif kind == "learned_linear":
            learner = LearnedLinearFusion(
                epochs=int(fusion_config.get("epochs", 200)),
                learning_rate=float(fusion_config.get("learning_rate", 0.08)),
                l2=float(fusion_config.get("l2", 0.01)),
                hard_negatives=int(fusion_config.get("hard_negatives", 20)),
            ).fit(source_runs, bundle.qrels, split.train)
            runs[name] = learner.run(
                source_runs,
                query_ids=tuple((*split.train, *split.validation, *split.test)),
                top_k=retrieval_depth,
            )
            method_metadata[name] = {
                **dict(fusion_config),
                "training_report": asdict(learner.report) if learner.report else {},
            }
        else:
            raise ResearchDataError(f"unsupported fusion type: {kind!r}")

    if not runs:
        raise ResearchDataError("configuration defines no baselines or imported runs")

    evaluations: dict[str, Any] = {}
    query_by_id = {query.query_id: query for query in bundle.queries}
    test_strata = build_query_strata(
        query_ids=split.test,
        qrels=bundle.qrels,
        queries=query_by_id,
        corpus=corpus,
    )
    strata_summary = summarize_strata(test_strata, query_count=len(split.test))
    identity_unsafe_ids = set(identity_unsafe_query_ids(leakage)) & set(split.test)
    identity_safe_test_ids = tuple(
        query_id for query_id in split.test if query_id not in identity_unsafe_ids
    )
    for name, run in runs.items():
        run_path = output_dir / "runs" / f"{name}.jsonl"
        write_run(run_path, run)
        result = evaluate_run(run, bundle.qrels, query_ids=split.test, ks=cutoffs)
        result["by_history_status"] = stratified_metrics(
            result, test_strata["history_status"]
        )
        result["by_profile_level"] = stratified_metrics(
            result, test_strata["profile_level"]
        )
        result["by_subject"] = stratified_metrics(result, test_strata["subject"])
        result["by_jcr_quartile"] = stratified_metrics(
            result, test_strata["jcr_quartile"]
        )
        # Backward-compatible aliases retained for existing analysis scripts.
        result["by_field"] = result["by_subject"]
        result["by_quartile"] = result["by_jcr_quartile"]
        result["identity_safe"] = (
            evaluate_run(
                run,
                bundle.qrels,
                query_ids=identity_safe_test_ids,
                ks=cutoffs,
            )
            if identity_safe_test_ids
            else {"query_count": 0, "cutoffs": list(cutoffs), "aggregate": {}, "per_query": {}}
        )
        evaluations[name] = result

    comparisons: list[dict[str, Any]] = []
    statistics_config = _mapping(config.get("statistics", {}), "statistics")
    for comparison in statistics_config.get("comparisons", ()):
        comparison = _mapping(comparison, "statistics.comparisons[]")
        left, right = str(comparison.get("left") or ""), str(comparison.get("right") or "")
        metric = str(comparison.get("metric") or "ndcg@10")
        if left not in evaluations or right not in evaluations:
            raise ResearchDataError(f"comparison references an unknown method: {left!r}, {right!r}")
        bootstrap_iterations = int(statistics_config.get("bootstrap_iterations", 10_000))
        permutation_iterations = int(statistics_config.get("permutation_iterations", 10_000))
        seed = int(statistics_config.get("seed", 20260814))
        comparison_result = {
                "left": left,
                "right": right,
                "bootstrap": paired_bootstrap_ci(
                    evaluations[left]["per_query"],
                    evaluations[right]["per_query"],
                    metric=metric,
                    iterations=bootstrap_iterations,
                    confidence=float(statistics_config.get("confidence", 0.95)),
                    seed=seed,
                ),
                "permutation": paired_permutation_test(
                    evaluations[left]["per_query"],
                    evaluations[right]["per_query"],
                    metric=metric,
                    iterations=permutation_iterations,
                    seed=seed,
                ),
            }
        if identity_safe_test_ids:
            comparison_result["identity_safe_bootstrap"] = paired_bootstrap_ci(
                evaluations[left]["identity_safe"]["per_query"],
                evaluations[right]["identity_safe"]["per_query"],
                metric=metric,
                iterations=bootstrap_iterations,
                confidence=float(statistics_config.get("confidence", 0.95)),
                seed=seed,
            )
            comparison_result["identity_safe_permutation"] = paired_permutation_test(
                evaluations[left]["identity_safe"]["per_query"],
                evaluations[right]["identity_safe"]["per_query"],
                metric=metric,
                iterations=permutation_iterations,
                seed=seed,
            )
        comparisons.append(comparison_result)

    manifest = build_data_manifest(
        dataset_path=dataset_path,
        corpus_path=corpus_path,
        bundle=bundle,
        corpus=corpus,
        split=split,
        config=config,
        additional_inputs=additional_inputs,
    )
    manifest["methods"] = method_metadata
    manifest["leakage_audit"] = {
        "passed": leakage["passed"],
        "severity_counts": leakage["severity_counts"],
    }
    _write_json(output_dir / "manifest.json", manifest)
    report = {
        "schema_version": 1,
        "manifest": "manifest.json",
        "leakage_audit": "leakage_audit.json",
        "evaluation_split": "test",
        "primary_evaluation": {
            "split": "test",
            "query_count": len(split.test),
            "denominator_policy": (
                "all frozen test queries remain in the primary denominator; "
                "strata are diagnostic views only"
            ),
        },
        "stratification": {
            "policy": STRATIFICATION_POLICY,
            "summary": strata_summary,
        },
        "identity_safe_test": {
            "policy": "exclude only queries whose input text explicitly contains the normalized gold venue name; full test remains primary",
            "full_query_count": len(split.test),
            "safe_query_count": len(identity_safe_test_ids),
            "excluded_query_count": len(identity_unsafe_ids),
            "excluded_query_ids": sorted(identity_unsafe_ids),
        },
        "methods": evaluations,
        "paired_comparisons": comparisons,
    }
    _write_json(output_dir / "metrics.json", report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Leakage-audited offline evaluation for Where Papers Go"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    evaluate = subparsers.add_parser("evaluate", help="run frozen retrieval baselines")
    evaluate.add_argument("--config", type=Path, required=True)

    cached = subparsers.add_parser(
        "build-cached-corpus", help="build temporal JSONL from local Crossref caches"
    )
    cached.add_argument("--cache-dir", type=Path, required=True)
    cached.add_argument("--jcr-csv", type=Path, required=True)
    cached.add_argument("--output-dir", type=Path, required=True)
    cached.add_argument("--start")
    cached.add_argument("--train-end", required=True)
    cached.add_argument("--dev-end", required=True)
    cached.add_argument("--test-end", required=True)
    cached.add_argument("--min-abstract-chars", type=int, default=100)
    cached.add_argument("--max-train-papers-per-venue", type=int, default=50)

    historical = subparsers.add_parser(
        "collect-historical-corpus",
        help="collect a PCL-assisted, multi-source historical profile for every JCR venue",
    )
    historical.add_argument("--api-config", type=Path, required=True)
    historical.add_argument("--jcr-csv", type=Path, required=True)
    historical.add_argument("--data-dir", type=Path, required=True)
    historical.add_argument("--output-dir", type=Path, required=True)
    historical.add_argument("--history-start", required=True)
    historical.add_argument("--cutoff", required=True)
    historical.add_argument("--mailto", default="rudykon@users.noreply.github.com")
    historical.add_argument("--batch-size", type=int, default=100)
    historical.add_argument("--workers", type=int, default=6)
    historical.add_argument(
        "--max-batches",
        type=int,
        default=0,
        help="0 processes the entire resumable queue; positive values cap this invocation",
    )
    historical.add_argument("--max-papers-per-venue", type=int, default=50)
    historical.add_argument("--min-papers-before-fallback", type=int, default=20)
    historical.add_argument("--max-pages-per-source", type=int, default=5)
    historical.add_argument(
        "--openalex-mode", choices=("always", "fallback", "off"), default="fallback"
    )
    historical.add_argument(
        "--scope-mode", choices=("always", "fallback", "off"), default="fallback"
    )
    historical.add_argument("--max-prototypes", type=int, default=8)
    historical.add_argument("--max-pcl-evidence", type=int, default=32)
    historical.add_argument(
        "--pcl-workers",
        type=int,
        default=3,
        help=(
            "independent PCL prototype queue workers; three is the measured "
            "production default"
        ),
    )
    historical.add_argument(
        "--scope-workers",
        type=int,
        default=1,
        help=(
            "independent official-scope LLM concurrency; keeping one reserved "
            "slot prevents PCL backlog from starving source collection"
        ),
    )
    historical.add_argument(
        "--pcl-models",
        nargs="+",
        default=None,
        help=(
            "ordered PCL model pool; values may also be comma-separated. "
            "Defaults to llm.pcl_models, llm.models, then llm.model"
        ),
    )
    historical.add_argument(
        "--pcl-model-fallbacks",
        type=int,
        default=None,
        help="different fallback models tried inside one queue attempt",
    )
    historical.add_argument(
        "--pcl-retries",
        type=int,
        default=2,
        help="exponential retries after the first PCL attempt",
    )
    historical.add_argument("--pcl-second-pass-attempts", type=int, default=2)
    historical.add_argument("--pcl-backoff-base", type=float, default=2.0)
    historical.add_argument("--pcl-backoff-max", type=float, default=30.0)
    historical.add_argument(
        "--pcl-max-tokens",
        type=int,
        default=8192,
        help=(
            "hard per-model completion ceiling; llm.max_output_tokens remains "
            "the ordinary-model base and model-specific limits may be lower"
        ),
    )
    historical.add_argument(
        "--retry-pcl-exhausted",
        action="store_true",
        help="explicitly requeue jobs that exhausted both recorded PCL passes",
    )
    historical.add_argument("--timeout", type=int, default=45)
    historical.add_argument("--retries", type=int, default=3)
    historical.add_argument("--crossref-request-interval", type=float, default=0.12)
    historical.add_argument("--openalex-request-interval", type=float, default=0.12)
    historical.add_argument("--retry-partial", action="store_true")
    historical.add_argument("--dry-run", action="store_true")
    historical.add_argument(
        "--smoke-limit",
        type=int,
        default=0,
        help="developer health check only: process 1--10 venues instead of a full batch",
    )
    historical.add_argument(
        "--smoke-venue-id",
        default="",
        help="developer health check only: select one exact jcr-* venue ID",
    )
    historical.add_argument(
        "--seed", default="where-papers-go-historical-venues-v1"
    )

    prototype_run = subparsers.add_parser(
        "build-prototype-vector-run",
        help="freeze a PCL bge-m3 max-pooled multi-prototype score run",
    )
    prototype_run.add_argument("--api-config", type=Path, required=True)
    prototype_run.add_argument("--dataset", type=Path, required=True)
    prototype_run.add_argument("--profiles", type=Path, required=True)
    prototype_run.add_argument("--output", type=Path, required=True)
    prototype_run.add_argument("--cache", type=Path, required=True)
    prototype_run.add_argument("--query-fields", nargs="+", default=("title", "abstract"))
    prototype_run.add_argument("--top-k", type=int, default=100)
    prototype_run.add_argument("--query-batch-size", type=int, default=16)
    prototype_run.add_argument("--prototype-chunk-size", type=int, default=4096)
    prototype_run.add_argument("--ignore-prototype-weights", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "evaluate":
            report = evaluate_config(args.config)
            print(json.dumps(
                {
                    name: result["aggregate"]
                    for name, result in report["methods"].items()
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ))
        elif args.command == "build-cached-corpus":
            manifest = build_cached_corpus(
                cache_dir=args.cache_dir,
                jcr_csv=args.jcr_csv,
                output_dir=args.output_dir,
                start=args.start,
                train_end=args.train_end,
                dev_end=args.dev_end,
                test_end=args.test_end,
                min_abstract_chars=args.min_abstract_chars,
                max_train_papers_per_venue=args.max_train_papers_per_venue,
            )
            print(json.dumps(manifest["coverage"], ensure_ascii=False, indent=2, sort_keys=True))
        elif args.command == "collect-historical-corpus":
            # Acquisition is networked, but its outputs are frozen before the
            # offline evaluator consumes them.  Credentials are never copied
            # into a manifest or attempt log.
            from where_paper_go import enrichment

            api_config = enrichment.load_api_config(args.api_config)
            pcl_semaphore = threading.BoundedSemaphore(max(1, args.pcl_workers))
            scope_semaphore = threading.BoundedSemaphore(max(1, args.scope_workers))
            pcl = PCLPrototypeClient(
                api_config,
                args.output_dir / "raw",
                max_output_tokens=args.pcl_max_tokens,
                request_semaphore=pcl_semaphore,
                models=args.pcl_models,
                model_fallbacks=args.pcl_model_fallbacks,
            )
            policy = CollectionPolicy(
                history_start=args.history_start,
                cutoff=args.cutoff,
                max_papers_per_venue=args.max_papers_per_venue,
                min_papers_before_fallback=args.min_papers_before_fallback,
                max_pages_per_source=args.max_pages_per_source,
                openalex_mode=args.openalex_mode,
                scope_mode=args.scope_mode,
                max_prototypes=args.max_prototypes,
                max_pcl_evidence=args.max_pcl_evidence,
            )
            venues = load_venue_seeds(args.jcr_csv, data_dir=args.data_dir)
            crossref_client = CachedJsonClient(
                args.output_dir / "raw",
                timeout=args.timeout,
                retries=args.retries,
                request_interval=args.crossref_request_interval,
            )
            openalex_client = CachedJsonClient(
                args.output_dir / "raw",
                timeout=args.timeout,
                retries=args.retries,
                request_interval=args.openalex_request_interval,
            )
            crossref = CrossrefHistoricalSource(crossref_client, mailto=args.mailto)
            openalex = (
                None
                if args.openalex_mode == "off"
                else OpenAlexHistoricalSource(openalex_client, mailto=args.mailto)
            )
            scope_search = (
                None
                if args.scope_mode == "off"
                else OfficialScopeSearchSource(
                    api_config,
                    args.output_dir / "raw" / "official_scope",
                    timeout=args.timeout,
                    llm_semaphore=scope_semaphore,
                )
            )
            manifest = run_historical_collection(
                venues=venues,
                policy=policy,
                output_dir=args.output_dir,
                jcr_csv=args.jcr_csv,
                crossref=crossref,
                openalex=openalex,
                scope_search=scope_search,
                pcl=pcl,
                batch_size=args.batch_size,
                workers=args.workers,
                max_batches=args.max_batches,
                smoke_limit=args.smoke_limit,
                smoke_venue_id=args.smoke_venue_id,
                seed=args.seed,
                retry_partial=args.retry_partial,
                pcl_retry_policy=PCLRetryPolicy(
                    max_attempts=args.pcl_retries + 1,
                    second_pass_attempts=args.pcl_second_pass_attempts,
                    backoff_base=args.pcl_backoff_base,
                    backoff_max=args.pcl_backoff_max,
                    workers=args.pcl_workers,
                ),
                retry_pcl_exhausted=args.retry_pcl_exhausted,
                dry_run=args.dry_run,
            )
            print(
                json.dumps(
                    manifest.get("coverage", manifest),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            bundle = load_recent_journal_dataset(
                args.dataset,
                query_fields=tuple(args.query_fields),
            )
            provider = pcl_embedding_provider(args.api_config)
            manifest = build_prototype_vector_run(
                provider=provider,
                bundle=bundle,
                profiles_path=args.profiles,
                cache_path=args.cache,
                output_path=args.output,
                top_k=args.top_k,
                query_batch_size=args.query_batch_size,
                prototype_chunk_size=args.prototype_chunk_size,
                apply_prototype_weights=not args.ignore_prototype_weights,
            )
            print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    except (OSError, ResearchDataError, HistoricalCollectionError, ValueError) as exc:
        print(f"research benchmark error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
