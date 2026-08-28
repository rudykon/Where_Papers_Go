"""Audited, non-overwriting acquisition of pinned Hugging Face model assets.

Every missing asset is dry-run before any download starts.  Successful payloads
are hashed in a unique shadow directory and atomically published; failed shadow
directories are retained with a failure record for diagnosis.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from typing import Any, Mapping, Sequence
import uuid

from .data import ResearchDataError, canonical_json_sha256, sha256_file


ASSET_MANIFEST_NAME = "ASSET_MANIFEST.json"
ASSET_MANIFEST_SCHEMA_VERSION = "1"
ACQUISITION_AUDIT_SCHEMA_VERSION = "1"
_REVISION_RE = re.compile(r"[0-9a-f]{40}")
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
_BEARER_SECRET_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_HF_TOKEN_RE = re.compile(r"\bhf_[A-Za-z0-9]{12,}\b")
_KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)\b(token|password|secret)=([^\s&]+)"
)


@dataclass(frozen=True)
class ModelAssetSpec:
    name: str
    repo_id: str
    revision: str
    include: tuple[str, ...]
    required_files: tuple[str, ...]
    estimated_download_bytes: int
    source_url: str
    configuration_sha256: str

    @property
    def directory_name(self) -> str:
        return f"{self.name}__{self.revision[:12]}"


def _safe_relative(value: object, *, field: str, allow_glob: bool) -> str:
    text = str(value or "").strip()
    if not text or "\x00" in text or text.startswith(("/", "\\")):
        raise ResearchDataError(f"model asset {field} must be a relative path")
    path = PurePosixPath(text.replace("\\", "/"))
    if ".." in path.parts:
        raise ResearchDataError(f"model asset {field} must not traverse parents")
    if not allow_glob and any(character in text for character in "*?[]"):
        raise ResearchDataError(f"model asset {field} must name an exact file")
    return path.as_posix()


def load_model_asset_config(
    path: Path,
) -> tuple[tuple[ModelAssetSpec, ...], dict[str, Any]]:
    """Load and strictly validate a pinned model-acquisition configuration."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchDataError(f"cannot read model asset config: {path}") from exc
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
        raise ResearchDataError("model asset config schema_version must be 1")
    raw_assets = raw.get("assets")
    if not isinstance(raw_assets, list) or not raw_assets:
        raise ResearchDataError("model asset config must contain assets")
    seen: set[str] = set()
    specs: list[ModelAssetSpec] = []
    for index, item in enumerate(raw_assets):
        if not isinstance(item, Mapping):
            raise ResearchDataError(f"model asset #{index} must be an object")
        name = str(item.get("name") or "").strip()
        repo_id = str(item.get("repo_id") or "").strip()
        revision = str(item.get("revision") or "").strip()
        if not _NAME_RE.fullmatch(name) or name in seen:
            raise ResearchDataError(f"invalid or duplicate model asset name: {name!r}")
        if (
            repo_id.count("/") != 1
            or any(part in {"", ".", ".."} for part in repo_id.split("/"))
            or not _REVISION_RE.fullmatch(revision)
        ):
            raise ResearchDataError(f"model asset {name!r} is not exactly pinned")
        raw_include = item.get("include")
        raw_required = item.get("required_files")
        if not isinstance(raw_include, list) or not raw_include:
            raise ResearchDataError(f"model asset {name!r} has no include patterns")
        if not isinstance(raw_required, list) or not raw_required:
            raise ResearchDataError(f"model asset {name!r} has no required files")
        include = tuple(
            _safe_relative(value, field="include", allow_glob=True)
            for value in raw_include
        )
        required = tuple(
            _safe_relative(value, field="required_files", allow_glob=False)
            for value in raw_required
        )
        for required_file in required:
            if not any(
                PurePosixPath(required_file).match(pattern) for pattern in include
            ):
                raise ResearchDataError(
                    f"model asset {name!r} required file {required_file!r} "
                    "is outside its include patterns"
                )
        try:
            estimated_bytes = int(item.get("estimated_download_bytes"))
        except (TypeError, ValueError) as exc:
            raise ResearchDataError(
                f"model asset {name!r} has no byte estimate"
            ) from exc
        if estimated_bytes < 1:
            raise ResearchDataError(f"model asset {name!r} has invalid byte estimate")
        source_url = str(item.get("source_url") or "").strip()
        expected_url = f"https://huggingface.co/{repo_id}/tree/{revision}"
        if source_url != expected_url:
            raise ResearchDataError(
                f"model asset {name!r} source_url must bind its exact revision"
            )
        seen.add(name)
        specs.append(
            ModelAssetSpec(
                name=name,
                repo_id=repo_id,
                revision=revision,
                include=include,
                required_files=required,
                estimated_download_bytes=estimated_bytes,
                source_url=source_url,
                configuration_sha256=canonical_json_sha256(dict(item)),
            )
        )
    config_record = {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "canonical_sha256": canonical_json_sha256(raw),
        "asset_count": len(specs),
        "estimated_download_bytes": sum(
            spec.estimated_download_bytes for spec in specs
        ),
    }
    configured_total = raw.get("estimated_total_download_bytes")
    if configured_total is not None:
        try:
            configured_total = int(configured_total)
        except (TypeError, ValueError) as exc:
            raise ResearchDataError(
                "model asset total byte estimate must be an integer"
            ) from exc
        if configured_total != config_record["estimated_download_bytes"]:
            raise ResearchDataError(
                "model asset total byte estimate does not equal its asset estimates"
            )
    return tuple(specs), config_record


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _payload_files(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ResearchDataError(f"model payload must not contain symlinks: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == ".cache":
            continue
        if relative.as_posix() in {ASSET_MANIFEST_NAME, "DOWNLOAD_FAILED.json"}:
            continue
        rows.append(
            {
                "path": relative.as_posix(),
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
        )
    if not rows:
        raise ResearchDataError(f"model payload contains no files: {root}")
    return rows


def _payload_record(root: Path, spec: ModelAssetSpec) -> dict[str, Any]:
    for relative in spec.required_files:
        candidate = root / relative
        if not candidate.is_file() or candidate.is_symlink():
            raise ResearchDataError(
                f"model asset {spec.name!r} lacks required file {relative!r}"
            )
    rows = _payload_files(root)
    return {
        "file_count": len(rows),
        "bytes": sum(int(row["bytes"]) for row in rows),
        "tree_sha256": canonical_json_sha256(rows),
        "files": rows,
    }


def _resolve_cli(value: str | Path) -> Path:
    text = str(value)
    located = shutil.which(text) if "/" not in text else text
    if not located:
        raise ResearchDataError(f"Hugging Face CLI is not available: {text}")
    path = Path(located).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ResearchDataError(f"Hugging Face CLI is not executable: {path}")
    return path


def _run_command(command: Sequence[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise ResearchDataError(f"cannot execute Hugging Face CLI: {command[0]}") from exc
    def redact(value: str) -> str:
        output = _BEARER_SECRET_RE.sub("[REDACTED]", value)
        output = _HF_TOKEN_RE.sub("[REDACTED]", output)
        output = _KEY_VALUE_SECRET_RE.sub(
            lambda match: f"{match.group(1)}=[REDACTED]", output
        )
        return output

    return {
        "command": [str(value) for value in command],
        "returncode": completed.returncode,
        "stdout": redact(completed.stdout[-200_000:]),
        "stderr": redact(completed.stderr[-200_000:]),
    }


def _parse_json_output(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        values: list[Any] = []
        for line in stripped.splitlines():
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError:
                return None
        return values


def _dry_run_entries(value: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    def visit(item: Any) -> None:
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if not isinstance(item, Mapping):
            return
        name = next(
            (
                str(item[key])
                for key in ("filename", "file_name", "path", "name")
                if item.get(key) not in (None, "")
            ),
            "",
        )
        raw_size = next(
            (
                item[key]
                for key in ("file_size", "size", "bytes", "download_size")
                if item.get(key) is not None
            ),
            None,
        )
        try:
            size = int(raw_size)
        except (TypeError, ValueError):
            size = -1
        if name and size >= 0:
            cached = item.get("is_cached", item.get("cached"))
            will_download = item.get("will_download")
            entries.append(
                {
                    "path": name,
                    "bytes": size,
                    "cached": bool(cached) if cached is not None else None,
                    "will_download": (
                        bool(will_download) if will_download is not None else None
                    ),
                }
            )
        for child in item.values():
            if isinstance(child, (list, Mapping)):
                visit(child)

    visit(value)
    deduplicated: dict[str, dict[str, Any]] = {}
    for entry in entries:
        deduplicated.setdefault(str(entry["path"]), entry)
    return [deduplicated[key] for key in sorted(deduplicated)]


def _dry_run_summary(record: Mapping[str, Any], fallback_bytes: int) -> dict[str, Any]:
    parsed = _parse_json_output(str(record.get("stdout") or ""))
    entries = _dry_run_entries(parsed)
    known_total = sum(int(entry["bytes"]) for entry in entries)
    cached_bytes = sum(
        int(entry["bytes"]) for entry in entries if entry.get("cached") is True
    )
    explicit_download = [
        entry for entry in entries if entry.get("will_download") is True
    ]
    if explicit_download:
        download_bytes = sum(int(entry["bytes"]) for entry in explicit_download)
    elif entries and all(entry.get("cached") is not None for entry in entries):
        download_bytes = known_total - cached_bytes
    else:
        download_bytes = fallback_bytes
    return {
        "parsed": bool(entries),
        "file_count": len(entries),
        "known_total_bytes": known_total,
        "cached_bytes": cached_bytes,
        "planned_download_bytes": download_bytes,
        "fallback_estimate_used": not bool(entries),
        "files": entries,
    }


def _base_download_command(
    cli: Path,
    spec: ModelAssetSpec,
    *,
    max_workers: int,
) -> list[str]:
    command = [
        str(cli),
        "download",
        spec.repo_id,
        "--revision",
        spec.revision,
    ]
    for pattern in spec.include:
        command.extend(("--include", pattern))
    command.extend(("--max-workers", str(max_workers), "--format", "json"))
    return command


def _validate_existing(target: Path, spec: ModelAssetSpec) -> dict[str, Any]:
    manifest_path = target / ASSET_MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchDataError(
            f"existing model asset is not auditable and will not be overwritten: {target}"
        ) from exc
    if not isinstance(manifest, Mapping):
        raise ResearchDataError(f"invalid existing model asset manifest: {manifest_path}")
    source = manifest.get("source")
    payload = manifest.get("payload")
    if (
        manifest.get("schema_version") != ASSET_MANIFEST_SCHEMA_VERSION
        or manifest.get("asset_configuration_sha256") != spec.configuration_sha256
        or not isinstance(source, Mapping)
        or source.get("repo_id") != spec.repo_id
        or source.get("revision") != spec.revision
        or not isinstance(payload, Mapping)
    ):
        raise ResearchDataError(
            f"existing model asset identity differs and will not be overwritten: {target}"
        )
    current = _payload_record(target, spec)
    if (
        current["tree_sha256"] != payload.get("tree_sha256")
        or current["bytes"] != payload.get("bytes")
        or current["file_count"] != payload.get("file_count")
    ):
        raise ResearchDataError(
            f"existing model asset payload hash mismatch: {target}"
        )
    return {
        "status": "validated_existing",
        "path": str(target.resolve()),
        "manifest_sha256": sha256_file(manifest_path),
        "payload": current,
    }


def materialize_model_assets(
    *,
    config_path: Path,
    output_root: Path,
    hf_cli: str | Path = "hf",
    execute: bool = False,
    authorization_reference: str = "",
    selected_assets: Sequence[str] = (),
    max_workers: int = 4,
    disk_safety_multiplier: float = 1.25,
    disk_reserve_bytes: int = 1_073_741_824,
    generation_command: Sequence[str] = (),
) -> dict[str, Any]:
    """Dry-run all missing assets, then optionally publish exact local copies."""

    if max_workers < 1 or max_workers > 32:
        raise ResearchDataError("model acquisition max_workers must be in [1, 32]")
    if disk_safety_multiplier < 1.0 or disk_reserve_bytes < 0:
        raise ResearchDataError("model acquisition disk safety settings are invalid")
    authorization = " ".join(authorization_reference.split())
    if execute and not authorization:
        raise ResearchDataError(
            "model download execution requires an explicit authorization reference"
        )
    specs, config_record = load_model_asset_config(config_path)
    requested = tuple(dict.fromkeys(str(value) for value in selected_assets))
    if requested:
        unknown = sorted(set(requested) - {spec.name for spec in specs})
        if unknown:
            raise ResearchDataError(f"unknown model assets requested: {unknown}")
        specs = tuple(spec for spec in specs if spec.name in requested)
    cli = _resolve_cli(hf_cli)
    version = _run_command((str(cli), "--version"))
    if version["returncode"] != 0:
        raise ResearchDataError("cannot determine Hugging Face CLI version")
    root = output_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    audit_path = (
        root
        / "_acquisition_audits"
        / f"model-assets-{timestamp}-{config_record['canonical_sha256'][:12]}.json"
    )
    audit: dict[str, Any] = {
        "schema_version": ACQUISITION_AUDIT_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "execute" if execute else "dry_run_only",
        "authorization_reference": authorization if execute else "not_required",
        "generation_command": [str(value) for value in generation_command],
        "config": config_record,
        "hf_cli": {
            "path": str(cli),
            "version_stdout": str(version["stdout"]).strip(),
            "version_stderr": str(version["stderr"]).strip(),
        },
        "cost_and_quota": {
            "known_provider_api_cost_usd": 0.0,
            "network_egress_cost": "not_estimated",
            "quota": "public Hub authentication and rate limits may apply",
        },
        "assets": [],
    }
    missing: list[tuple[ModelAssetSpec, Path, dict[str, Any]]] = []
    try:
        for spec in specs:
            target = root / spec.directory_name
            if target.exists():
                existing = _validate_existing(target, spec)
                audit["assets"].append(
                    {
                        "name": spec.name,
                        "repo_id": spec.repo_id,
                        "revision": spec.revision,
                        **existing,
                    }
                )
                continue
            command = _base_download_command(cli, spec, max_workers=max_workers)
            command.extend(("--dry-run",))
            dry_run = _run_command(command)
            summary = _dry_run_summary(dry_run, spec.estimated_download_bytes)
            planned_files = {
                str(item["path"]) for item in summary["files"]
            }
            missing_required = (
                sorted(set(spec.required_files) - planned_files)
                if summary["parsed"]
                else []
            )
            record = {
                "name": spec.name,
                "repo_id": spec.repo_id,
                "revision": spec.revision,
                "source_url": spec.source_url,
                "status": (
                    "dry_run_passed"
                    if dry_run["returncode"] == 0 and not missing_required
                    else "dry_run_failed"
                ),
                "configured_estimated_download_bytes": spec.estimated_download_bytes,
                "dry_run": {
                    **dry_run,
                    "summary": summary,
                    "missing_required_files": missing_required,
                },
            }
            audit["assets"].append(record)
            if dry_run["returncode"] != 0 or missing_required:
                audit["status"] = "failed_before_download"
                _atomic_json(audit_path, audit)
                raise ResearchDataError(
                    f"Hugging Face dry-run failed or omitted required files for "
                    f"{spec.name}; audit: {audit_path}"
                )
            missing.append((spec, target, record))

        planned_download_bytes = sum(
            int(record["dry_run"]["summary"]["planned_download_bytes"])
            for _spec, _target, record in missing
        )
        disk = shutil.disk_usage(root)
        required_free = int(planned_download_bytes * disk_safety_multiplier) + int(
            disk_reserve_bytes
        )
        audit["preflight"] = {
            "missing_asset_count": len(missing),
            "validated_existing_asset_count": len(specs) - len(missing),
            "planned_download_bytes": planned_download_bytes,
            "disk_free_bytes": disk.free,
            "disk_required_bytes": required_free,
            "disk_safety_multiplier": disk_safety_multiplier,
            "disk_reserve_bytes": disk_reserve_bytes,
            "cache_coverage_bytes": sum(
                int(record["dry_run"]["summary"]["cached_bytes"])
                for _spec, _target, record in missing
            ),
        }
        if execute and disk.free < required_free:
            audit["status"] = "insufficient_disk"
            _atomic_json(audit_path, audit)
            raise ResearchDataError(
                f"insufficient disk for model downloads; audit: {audit_path}"
            )
        if not execute:
            audit["status"] = "dry_run_complete"
            _atomic_json(audit_path, audit)
            audit["audit_path"] = str(audit_path)
            audit["audit_sha256"] = sha256_file(audit_path)
            return audit

        for spec, target, record in missing:
            building = root / (
                f".{spec.directory_name}.building-{timestamp}-{uuid.uuid4().hex[:8]}"
            )
            if building.exists():
                raise ResearchDataError(f"model shadow path already exists: {building}")
            building.mkdir(parents=False)
            record["shadow_path"] = str(building)
            command = _base_download_command(cli, spec, max_workers=max_workers)
            command.extend(("--local-dir", str(building)))
            download = _run_command(command)
            record["download"] = download
            if download["returncode"] != 0:
                record["status"] = "download_failed_shadow_preserved"
                failure = {
                    "schema_version": "1",
                    "asset": spec.name,
                    "repo_id": spec.repo_id,
                    "revision": spec.revision,
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                    "download": download,
                }
                _atomic_json(building / "DOWNLOAD_FAILED.json", failure)
                audit["status"] = "download_failed"
                _atomic_json(audit_path, audit)
                raise ResearchDataError(
                    f"model download failed; shadow preserved at {building}"
                )
            try:
                payload = _payload_record(building, spec)
                manifest = {
                    "schema_version": ASSET_MANIFEST_SCHEMA_VERSION,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "asset_name": spec.name,
                    "asset_configuration_sha256": spec.configuration_sha256,
                    "source": {
                        "repo_id": spec.repo_id,
                        "revision": spec.revision,
                        "source_url": spec.source_url,
                        "include": list(spec.include),
                    },
                    "payload": payload,
                    "acquisition": {
                        "hf_cli_path": str(cli),
                        "hf_cli_version": str(version["stdout"]).strip(),
                        "dry_run_command": record["dry_run"]["command"],
                        "download_command": download["command"],
                        "authorization_reference": authorization,
                        "known_provider_api_cost_usd": 0.0,
                    },
                    "final_path": str(target),
                }
                _atomic_json(building / ASSET_MANIFEST_NAME, manifest)
                if target.exists():
                    raise ResearchDataError(
                        "refusing to replace model asset created concurrently: "
                        f"{target}"
                    )
                os.replace(building, target)
            except Exception as exc:
                record["status"] = "validation_failed_shadow_preserved"
                failure = {
                    "schema_version": "1",
                    "asset": spec.name,
                    "repo_id": spec.repo_id,
                    "revision": spec.revision,
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                    "failure_type": type(exc).__name__,
                    "failure_message": str(exc),
                }
                _atomic_json(building / "DOWNLOAD_FAILED.json", failure)
                audit["status"] = "validation_failed"
                _atomic_json(audit_path, audit)
                raise
            record["status"] = "published"
            record["path"] = str(target)
            record["manifest_sha256"] = sha256_file(target / ASSET_MANIFEST_NAME)
            record["payload"] = payload
        audit["status"] = "complete"
        _atomic_json(audit_path, audit)
        audit["audit_path"] = str(audit_path)
        audit["audit_sha256"] = sha256_file(audit_path)
        return audit
    except Exception:
        if not audit_path.exists():
            audit["status"] = audit.get("status", "failed")
            _atomic_json(audit_path, audit)
        raise
