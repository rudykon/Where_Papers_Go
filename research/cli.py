"""Command-line entry point for the frozen offline research benchmark."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import shlex
import sys
import threading
from typing import Any, Mapping, Sequence

from .baselines import BM25Baseline, ImportedRunBaseline, TfidfBaseline
from .cache_builder import build_cached_corpus
from .clean_corpus import rebuild_clean_corpus
from .data import (
    ResearchDataError,
    build_data_manifest,
    build_run_binding,
    canonical_json_sha256,
    exclude_query_identities_from_prototypes,
    load_evidence_concat_corpus,
    load_jcr_corpus,
    load_jsonl_corpus,
    load_recent_journal_dataset,
    load_score_run,
    runtime_provenance,
    sha256_file,
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
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _implementation_revision(runtime: Mapping[str, Any]) -> str:
    code = _mapping(runtime.get("code"), "runtime.code")
    return ":".join(
        str(code.get(field) or "")
        for field in ("commit", "status_sha256", "tracked_diff_sha256")
    )


def evaluate_config(
    config_path: Path, *, command: Sequence[str] | None = None
) -> dict[str, Any]:
    config_path = config_path.resolve()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchDataError(f"cannot read configuration: {config_path}") from exc
    config = _mapping(config, "root")
    if config.get("offline_only") is not True:
        raise ResearchDataError("research evaluation requires offline_only=true")
    recorded_command = tuple(
        str(value)
        for value in (
            command
            or (
                sys.executable,
                "-m",
                "research",
                "evaluate",
                "--config",
                str(config_path),
            )
        )
    )
    reproduction = {
        "command": list(recorded_command),
        "shell_command": shlex.join(recorded_command),
        "working_directory": str(Path.cwd().resolve()),
    }

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

    query_by_id = {query.query_id: query for query in bundle.queries}

    def exclusion_queries(field: str) -> tuple[Any, ...]:
        raw_splits = corpus_config.get(field)
        if not isinstance(raw_splits, list) or not raw_splits:
            raise ResearchDataError(f"corpus.{field} must be a non-empty list")
        allowed = {"validation", "test"}
        names = tuple(str(value) for value in raw_splits)
        unknown = set(names) - allowed
        if unknown:
            raise ResearchDataError(
                f"corpus.{field} may contain only validation/test; "
                f"unknown={sorted(unknown)}"
            )
        query_ids = tuple(
            query_id for name in names for query_id in getattr(split, name)
        )
        return tuple(query_by_id[query_id] for query_id in query_ids)

    corpus_type = str(corpus_config.get("type") or "jsonl")
    corpus_additional_inputs: list[Path] = []
    corpus_exclusion: dict[str, Any] | None = None
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
        if corpus_config.get("prototype_identity_exclusion_splits") is not None:
            corpus, corpus_exclusion = exclude_query_identities_from_prototypes(
                corpus,
                excluded_queries=exclusion_queries(
                    "prototype_identity_exclusion_splits"
                ),
            )
    elif corpus_type == "evidence_jsonl":
        evidence_path = _resolve(config_path, corpus_config.get("evidence_path"))
        corpus, corpus_exclusion = load_evidence_concat_corpus(
            corpus_path,
            evidence_path,
            excluded_queries=exclusion_queries("identity_exclusion_splits"),
            id_field=str(corpus_config.get("id_field") or "venue_id"),
            snapshot_field=str(
                corpus_config.get("snapshot_field") or "snapshot_date"
            ),
        )
        corpus_additional_inputs.append(evidence_path)
    else:
        raise ResearchDataError(f"unsupported corpus type: {corpus_type!r}")

    output_dir.mkdir(parents=True, exist_ok=True)
    if corpus_exclusion is not None:
        _write_json(output_dir / "corpus_exclusion_audit.json", corpus_exclusion)

    corpus_views: set[str] = set()
    for baseline_config in config.get("baselines", ()):
        baseline_config = _mapping(baseline_config, "baselines[]")
        corpus_views.add(
            "prototypes"
            if bool(baseline_config.get("use_prototypes", False))
            else "document"
        )
    for imported_config in config.get("imported_runs", ()):
        imported_config = _mapping(imported_config, "imported_runs[]")
        declared_view = str(imported_config.get("corpus_view") or "").strip()
        if declared_view:
            corpus_views.add(declared_view)
        else:
            # An undeclared external scorer is audited against every possible
            # local input surface.  This is conservative and fail-closed.
            corpus_views.update(("document", "metadata_sources", "prototypes"))
    if not corpus_views:
        corpus_views.update(("document", "metadata_sources", "prototypes"))

    leakage = audit_leakage(
        bundle,
        corpus,
        split,
        corpus_views=tuple(sorted(corpus_views)),
    )
    _write_json(output_dir / "leakage_audit.json", leakage)
    if config.get("fail_on_critical_leakage", True) and not leakage["passed"]:
        raise ResearchDataError(
            f"critical leakage found; inspect {output_dir / 'leakage_audit.json'}"
        )

    active_query_ids = set((*split.train, *split.validation, *split.test))
    queries = [
        query for query in bundle.queries if query.query_id in active_query_ids
    ]
    ordered_query_ids = tuple(query.query_id for query in queries)
    candidate_ids = tuple(sorted(document.doc_id for document in corpus))
    retrieval_depth = int(evaluation_config.get("retrieval_depth") or 100)
    cutoffs = tuple(int(value) for value in evaluation_config.get("cutoffs", (1, 3, 5, 10, 20, 50)))
    if retrieval_depth < max(cutoffs):
        raise ResearchDataError("retrieval_depth must be at least the largest evaluation cutoff")

    runtime = runtime_provenance()
    binding = build_run_binding(
        dataset_path=dataset_path,
        profiles_path=corpus_path,
        query_ids=ordered_query_ids,
        candidate_ids=candidate_ids,
        configuration=config,
        configuration_path=config_path,
        additional_input_paths=tuple(corpus_additional_inputs),
    )
    implementation_revision = _implementation_revision(runtime)

    runs: dict[str, Run] = {}
    method_metadata: dict[str, Any] = {}
    method_identities: dict[str, dict[str, Any]] = {}
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
        method_identities[name] = {
            "name": name,
            "kind": kind,
            "implementation": f"research.baselines.{type(baseline).__name__}",
            "implementation_revision": implementation_revision,
            "configuration_sha256": canonical_json_sha256(baseline_config),
        }

    additional_inputs: list[Path] = list(corpus_additional_inputs)
    for imported_config in config.get("imported_runs", ()):
        imported_config = _mapping(imported_config, "imported_runs[]")
        name = str(imported_config.get("name") or "").strip()
        if not name or name in runs:
            raise ResearchDataError(f"duplicate or empty imported run name: {name!r}")
        path = _resolve(config_path, imported_config.get("path"))
        manifest_path = (
            _resolve(config_path, imported_config.get("manifest_path"))
            if imported_config.get("manifest_path")
            else path.with_suffix(path.suffix + ".manifest.json")
        )
        manifest_sha256 = str(imported_config.get("manifest_sha256") or "").strip()
        generation_config_sha256 = str(
            imported_config.get("generation_config_sha256") or ""
        ).strip()
        expected_identity = {
            key: str(imported_config[key]).strip()
            for key in (
                "model_revision",
                "provider_fingerprint",
                "implementation_revision",
            )
            if str(imported_config.get(key) or "").strip()
        }
        if not manifest_sha256 or not generation_config_sha256 or not expected_identity:
            raise ResearchDataError(
                f"imported run {name!r} requires manifest_sha256, "
                "generation_config_sha256, and an exact method identity"
            )
        imported = load_score_run(
            path,
            expected_query_ids=ordered_query_ids,
            candidate_ids=candidate_ids,
            expected_binding=binding,
            expected_manifest_sha256=manifest_sha256,
            expected_configuration_sha256=generation_config_sha256,
            expected_method_identity=expected_identity,
            manifest_path=manifest_path,
            top_k=retrieval_depth,
        )
        adapter = ImportedRunBaseline(imported, name=name)
        runs[name] = adapter.fit(corpus).run(queries, top_k=retrieval_depth)
        additional_inputs.extend((path, manifest_path))
        method_metadata[name] = dict(imported_config)
        method_identities[name] = {
            "name": name,
            "kind": str(imported_config.get("type") or "imported"),
            **expected_identity,
            "source_run_sha256": sha256_file(path),
            "source_manifest_sha256": manifest_sha256,
            "configuration_sha256": canonical_json_sha256(imported_config),
        }

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
        method_identities[name] = {
            "name": name,
            "kind": kind,
            "implementation": (
                "research.fusion.rrf_fuse"
                if kind == "rrf"
                else "research.fusion.LearnedLinearFusion"
            ),
            "implementation_revision": implementation_revision,
            "configuration_sha256": canonical_json_sha256(fusion_config),
            "source_methods_sha256": canonical_json_sha256(
                {source: method_identities[source] for source in source_names}
            ),
        }

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
    frozen_runs: dict[str, Any] = {}
    for name, run in runs.items():
        run_path = output_dir / "runs" / f"{name}.jsonl"
        run_manifest = write_run(
            run_path,
            run,
            binding=binding,
            query_ids=ordered_query_ids,
            candidate_ids=candidate_ids,
            top_k=retrieval_depth,
            method=method_identities[name],
            command=recorded_command,
            working_directory=Path.cwd(),
            runtime=runtime,
        )
        run_manifest_path = run_path.with_suffix(run_path.suffix + ".manifest.json")
        frozen_runs[name] = {
            "run": {
                "path": str(run_path.resolve()),
                "sha256": run_manifest["output"]["sha256"],
                "bytes": run_manifest["output"]["bytes"],
            },
            "manifest": {
                "path": str(run_manifest_path.resolve()),
                "sha256": sha256_file(run_manifest_path),
                "bytes": run_manifest_path.stat().st_size,
            },
            "coverage": run_manifest["coverage"],
            "method": run_manifest["method"],
        }
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
        for dimension in (
            "by_history_status",
            "by_profile_level",
            "by_subject",
            "by_jcr_quartile",
        ):
            assigned = sum(
                int(group["query_count"]) for group in result[dimension].values()
            )
            if assigned != len(split.test):
                raise ResearchDataError(
                    f"method {name!r} stratum {dimension!r} covers "
                    f"{assigned} queries; expected {len(split.test)}"
                )
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

    report = {
        "schema_version": 2,
        "manifest": "manifest.json",
        "leakage_audit": "leakage_audit.json",
        "reproduction": reproduction,
        "frozen_run_contract": {
            "query_count": len(ordered_query_ids),
            "ordered_query_ids_sha256": binding["queries"][
                "ordered_ids_sha256"
            ],
            "candidate_universe_count": len(candidate_ids),
            "candidate_ids_sha256": binding["candidates"][
                "ordered_ids_sha256"
            ],
            "all_methods_share_binding": True,
        },
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
    metrics_path = output_dir / "metrics.json"
    leakage_path = output_dir / "leakage_audit.json"
    _write_json(metrics_path, report)
    outputs = {
        "metrics": {
            "path": str(metrics_path.resolve()),
            "sha256": sha256_file(metrics_path),
            "bytes": metrics_path.stat().st_size,
        },
        "leakage_audit": {
            "path": str(leakage_path.resolve()),
            "sha256": sha256_file(leakage_path),
            "bytes": leakage_path.stat().st_size,
        },
    }
    if corpus_exclusion is not None:
        exclusion_path = output_dir / "corpus_exclusion_audit.json"
        outputs["corpus_exclusion_audit"] = {
            "path": str(exclusion_path.resolve()),
            "sha256": sha256_file(exclusion_path),
            "bytes": exclusion_path.stat().st_size,
        }
    manifest = build_data_manifest(
        config_path=config_path,
        dataset_path=dataset_path,
        corpus_path=corpus_path,
        bundle=bundle,
        corpus=corpus,
        split=split,
        config=config,
        binding=binding,
        runtime=runtime,
        reproduction=reproduction,
        frozen_runs=frozen_runs,
        outputs=outputs,
        additional_inputs=additional_inputs,
    )
    manifest["methods"] = method_metadata
    manifest["leakage_audit"] = {
        "passed": leakage["passed"],
        "severity_counts": leakage["severity_counts"],
    }
    _write_json(output_dir / "manifest.json", manifest)
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
    prototype_run.add_argument("--reference-manifest", type=Path, required=True)
    prototype_run.add_argument("--output", type=Path, required=True)
    prototype_run.add_argument("--cache", type=Path, required=True)
    prototype_run.add_argument("--query-fields", nargs="+", default=("title", "abstract"))
    prototype_run.add_argument("--top-k", type=int, default=100)
    prototype_run.add_argument("--query-batch-size", type=int, default=16)
    prototype_run.add_argument("--prototype-chunk-size", type=int, default=4096)
    prototype_run.add_argument("--ignore-prototype-weights", action="store_true")

    clean = subparsers.add_parser(
        "rebuild-clean-corpus",
        help="derive a causal research corpus from stored acquisition shards only",
    )
    clean.add_argument("--source-dir", type=Path, required=True)
    clean.add_argument("--output-dir", type=Path, required=True)
    clean.add_argument("--jcr-csv", type=Path, required=True)
    clean.add_argument("--data-dir", type=Path, required=True)
    clean.add_argument("--history-start", required=True)
    clean.add_argument("--cutoff", required=True)
    clean.add_argument("--mode", choices=("deterministic", "pcl"), required=True)
    clean.add_argument("--api-config", type=Path)
    clean.add_argument("--pcl-cache-dir", type=Path)
    clean.add_argument("--workers", type=int, default=3)
    clean.add_argument("--max-prototypes", type=int, default=8)
    clean.add_argument("--max-pcl-evidence", type=int, default=32)
    clean.add_argument("--pcl-max-tokens", type=int, default=8192)
    clean.add_argument("--pcl-models", nargs="+", default=None)
    clean.add_argument("--pcl-model-fallbacks", type=int, default=None)
    clean.add_argument("--pcl-prototypes-per-venue", type=int, default=None)
    clean.add_argument("--pcl-retries", type=int, default=2)
    clean.add_argument("--pcl-backoff-base", type=float, default=2.0)
    clean.add_argument("--pcl-backoff-max", type=float, default=30.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = tuple(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(raw_argv)
    recorded_command = (sys.executable, "-m", "research", *raw_argv)
    try:
        if args.command == "evaluate":
            report = evaluate_config(args.config, command=recorded_command)
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
        elif args.command == "rebuild-clean-corpus":
            policy = CollectionPolicy(
                history_start=args.history_start,
                cutoff=args.cutoff,
                max_prototypes=args.max_prototypes,
                max_pcl_evidence=args.max_pcl_evidence,
            )
            venues = load_venue_seeds(args.jcr_csv, data_dir=args.data_dir)
            pcl = None
            if args.mode == "pcl":
                if args.api_config is None:
                    raise ResearchDataError("clean PCL rebuild requires --api-config")
                from where_paper_go import enrichment

                api_config = enrichment.load_api_config(args.api_config)
                cache_dir = args.pcl_cache_dir or (
                    args.source_dir / "raw_clean_temporal_pcl"
                )
                pcl = PCLPrototypeClient(
                    api_config,
                    cache_dir,
                    max_output_tokens=args.pcl_max_tokens,
                    request_semaphore=threading.BoundedSemaphore(args.workers),
                    models=args.pcl_models,
                    model_fallbacks=args.pcl_model_fallbacks,
                    prototypes_per_venue=args.pcl_prototypes_per_venue,
                )
            manifest = rebuild_clean_corpus(
                venues=venues,
                policy=policy,
                source_dir=args.source_dir,
                output_dir=args.output_dir,
                jcr_csv=args.jcr_csv,
                mode=args.mode,
                pcl=pcl,
                workers=args.workers,
                pcl_attempts=args.pcl_retries + 1,
                pcl_backoff_base=args.pcl_backoff_base,
                pcl_backoff_max=args.pcl_backoff_max,
            )
            print(
                json.dumps(
                    {
                        "coverage": manifest["coverage"],
                        "validation": manifest["validation"],
                        "observed_pcl_model_distribution": manifest[
                            "observed_pcl_model_distribution"
                        ],
                    },
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
            last_progress_bucket = -1

            def report_embedding_progress(processed: int, total: int) -> None:
                nonlocal last_progress_bucket
                bucket = processed // 1024
                if processed != total and bucket == last_progress_bucket:
                    return
                last_progress_bucket = bucket
                print(
                    json.dumps(
                        {
                            "embedding_progress": {
                                "processed": processed,
                                "total": total,
                            }
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                    flush=True,
                )

            manifest = build_prototype_vector_run(
                provider=provider,
                bundle=bundle,
                dataset_path=args.dataset,
                profiles_path=args.profiles,
                cache_path=args.cache,
                output_path=args.output,
                top_k=args.top_k,
                query_batch_size=args.query_batch_size,
                prototype_chunk_size=args.prototype_chunk_size,
                apply_prototype_weights=not args.ignore_prototype_weights,
                query_fields=tuple(args.query_fields),
                reference_manifest_path=args.reference_manifest,
                embedding_progress=report_embedding_progress,
                generation_command=recorded_command,
            )
            print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    except (OSError, ResearchDataError, HistoricalCollectionError, ValueError) as exc:
        print(f"research benchmark error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
