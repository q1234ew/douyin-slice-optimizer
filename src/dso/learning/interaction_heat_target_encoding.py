from __future__ import annotations

import hashlib
import json
import math
import random
import re
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import blake2b
from pathlib import Path
from typing import Mapping, Sequence

from dso.learning.interaction_heat_v3 import verify_interaction_heat_artifact
from dso.versions import INTERACTION_HEAT_TARGET_ENCODING_VERSION


_CORE_METADATA_FIELDS = (
    "content_category",
    "hook_type",
    "slice_structure",
    "media_type",
    "classification_confidence",
    "structure_confidence",
    "entity_signal",
    "is_original_sound",
)
_NUMERIC_BUCKET_FIELDS = {
    "classification_confidence",
    "structure_confidence",
}
DEFAULT_TARGET_ENCODING_EXPERIMENT_ID = (
    "dso-interaction-heat-target-encoding-20260720-r2"
)
_EXPERIMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,95}$")


@dataclass(frozen=True)
class TargetEncodingConfig:
    alpha: float = 20.0
    min_samples: int = 3
    folds: int = 5
    seed: int = 20260720
    include_title: bool = False


@dataclass(frozen=True)
class TargetEncodingPrediction:
    score: float
    feature_count: int
    fallback_counts: Mapping[str, int]


@dataclass(frozen=True)
class TargetEncodingModel:
    protocol: str
    include_account_history: bool
    alpha: float
    min_samples: int
    global_mean: float
    field_stats: Mapping[str, Mapping[str, tuple[int, float]]]
    account_field_stats: Mapping[str, Mapping[str, tuple[int, float]]]
    include_title: bool
    training_sample_count: int


def run_local_target_encoding_experiment(
    *,
    experiment_id: str = DEFAULT_TARGET_ENCODING_EXPERIMENT_ID,
    label_artifact_dir: Path,
    expected_label_manifest_sha256: str,
    db_path: Path,
    output_root: Path,
    config: TargetEncodingConfig | None = None,
    evaluation_scope: str = "validation",
) -> dict:
    if not _EXPERIMENT_ID_RE.fullmatch(experiment_id):
        raise ValueError("invalid target encoding experiment_id")
    if evaluation_scope not in {"validation", "all"}:
        raise ValueError("target encoding evaluation_scope must be validation or all")
    target = Path(output_root).resolve() / experiment_id
    if target.exists():
        raise FileExistsError(f"target encoding experiment already exists: {target}")
    label_root = Path(label_artifact_dir).resolve()
    verification = verify_interaction_heat_artifact(
        label_root,
        expected_manifest_sha256=expected_label_manifest_sha256,
    )
    if not verification["passed"]:
        raise ValueError("interaction heat label artifact verification failed")
    settings = config or TargetEncodingConfig()
    _validate_config(settings)
    splits = _read_jsonl_index(label_root / "splits.jsonl", "sample_id")
    allowed_splits = {"train", "validation"}
    if evaluation_scope == "all":
        allowed_splits.add("test")
    eligible_ids = {
        sample_id
        for sample_id, split in splits.items()
        if any(
            str(split[f"{protocol}_split"]) in allowed_splits
            for protocol in ("account_time", "account_holdout")
        )
    }
    labels = _read_jsonl_index(
        label_root / "labels.jsonl",
        "sample_id",
        allowed_values=eligible_ids,
    )
    if set(labels) != eligible_ids:
        raise ValueError("interaction heat labels are missing eligible split samples")
    metadata = _load_safe_metadata(Path(db_path), eligible_ids)
    missing_metadata = sorted(eligible_ids.difference(metadata))
    if missing_metadata:
        raise ValueError(
            f"historical metadata is missing {len(missing_metadata)} labeled samples"
        )

    models: dict[str, dict] = {}
    protocol_reports: dict[str, dict] = {}
    predictions: list[dict] = []
    for protocol in ("account_time", "account_holdout"):
        protocol_rows = _protocol_rows(
            protocol,
            labels=labels,
            splits=splits,
            metadata=metadata,
            allowed_splits=allowed_splits,
        )
        training_rows = [row for row in protocol_rows if row["split"] == "train"]
        model = fit_target_encoder(
            training_rows,
            protocol=protocol,
            config=settings,
        )
        models[protocol] = _model_payload(model)
        oof_predictions = cross_fit_target_encoder(
            training_rows,
            protocol=protocol,
            config=settings,
        )
        oof_scored = [
            dict(row, score=oof_predictions[str(row["sample_id"])].score)
            for row in training_rows
        ]
        split_reports = {"train_oof": _ranking_metrics(oof_scored, seed=settings.seed)}
        for row in training_rows:
            prediction = oof_predictions[str(row["sample_id"])]
            predictions.append(
                _prediction_payload(protocol, "train_oof", row, prediction)
            )
        evaluation_splits = ["validation"]
        if evaluation_scope == "all":
            evaluation_splits.append("test")
        for split_name in evaluation_splits:
            evaluation_rows = [
                row for row in protocol_rows if row["split"] == split_name
            ]
            scored = []
            for row in evaluation_rows:
                prediction = predict_target_encoding(model, row)
                scored.append(dict(row, score=prediction.score))
                predictions.append(
                    _prediction_payload(protocol, split_name, row, prediction)
                )
            split_reports[split_name] = _ranking_metrics(
                scored,
                seed=settings.seed + (1 if split_name == "test" else 0),
            )
        protocol_reports[protocol] = {
            "splits": split_reports,
            "train_sample_count": len(training_rows),
        }

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{experiment_id}-", dir=target.parent))
    try:
        model_path = staging / "model.json"
        predictions_path = staging / "predictions.jsonl"
        report_path = staging / "report.json"
        model_payload = {
            "config": _config_payload(settings),
            "feature_contract": {
                "forbidden": [
                    "likes",
                    "comments",
                    "favorites",
                    "shares",
                    "reward_proxy",
                    "normalized_reward",
                    "performance_label",
                    "all direct interaction-outcome derivatives",
                ],
                "metadata_fields": list(_CORE_METADATA_FIELDS),
                "title_features": (
                    "normalized length bucket + first 16 stable character bigrams"
                    if settings.include_title
                    else "disabled"
                ),
            },
            "model_version": INTERACTION_HEAT_TARGET_ENCODING_VERSION,
            "models": models,
        }
        report_payload = {
            "admission": "research_only",
            "claim_limit": (
                "Offline ranking of observed interaction-heat proxies only; no claim "
                "about play traffic, exposure-normalized conversion, or production lift."
            ),
            "evaluation_scope": evaluation_scope,
            "experiment_id": experiment_id,
            "protocols": protocol_reports,
            "status": "completed",
            "test_policy": (
                "observed_non_promotable"
                if evaluation_scope == "all"
                else "sealed_not_loaded_by_this_experiment"
            ),
        }
        _write_json(model_path, model_payload)
        predictions_path.write_text(
            "".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                + "\n"
                for row in sorted(
                    predictions,
                    key=lambda row: (
                        str(row["protocol"]),
                        str(row["split"]),
                        str(row["sample_id"]),
                    ),
                )
            ),
            encoding="utf-8",
        )
        _write_json(report_path, report_payload)
        label_manifest = json.loads((label_root / "manifest.json").read_text())
        files = {
            path.name: _file_sha256(path)
            for path in (model_path, predictions_path, report_path)
        }
        manifest_payload = {
            "admission": "research_only",
            "config": _config_payload(settings),
            "contract_version": "interaction_heat_target_encoding_experiment.v1",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "database_sha256": _file_sha256(Path(db_path)),
            "effective_model_cost_cny": "0.000000",
            "evaluation_scope": evaluation_scope,
            "experiment_id": experiment_id,
            "files": files,
            "label_artifact_id": label_manifest.get("artifact_id"),
            "label_manifest_sha256": expected_label_manifest_sha256.lower(),
            "model_version": INTERACTION_HEAT_TARGET_ENCODING_VERSION,
            "network_request_count": 0,
            "production_impact": {
                "automatic_export": False,
                "automatic_publish": False,
                "database_rows_updated": 0,
                "manual_gold_changed": False,
                "production_weight_changed": False,
            },
            "source_code_sha256": _file_sha256(Path(__file__)),
            "status": "completed",
        }
        manifest_payload["manifest_sha256"] = hashlib.sha256(
            json.dumps(
                manifest_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        _write_json(staging / "manifest.json", manifest_payload)
        if target.exists():
            raise FileExistsError(f"target encoding experiment already exists: {target}")
        staging.rename(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "artifact_dir": str(target),
        "evaluation_scope": evaluation_scope,
        "experiment_id": experiment_id,
        "manifest_sha256": manifest_payload["manifest_sha256"],
        "network_request_count": 0,
        "production_weight_changed": False,
        "status": "completed",
    }


def extract_target_encoding_fields(
    row: Mapping,
    *,
    include_title: bool = False,
) -> dict[str, tuple[str, ...]]:
    metadata_value = row.get("metadata")
    metadata = metadata_value if isinstance(metadata_value, Mapping) else {}
    published = datetime.fromisoformat(
        str(row["published_at"]).replace("Z", "+00:00")
    )
    fields: dict[str, tuple[str, ...]] = {
        "duration_bucket": (str(row.get("duration_bucket") or "unknown"),),
        "publication_age_bucket": (
            str(row.get("publication_age_bucket") or "unknown"),
        ),
        "published_month": (str(published.month),),
        "published_weekday": (str(published.weekday()),),
        "published_hour_bucket": (str(published.hour // 4),),
    }
    confidence_value = row.get("confidence")
    confidence = confidence_value if isinstance(confidence_value, Mapping) else {}
    grade = str(confidence.get("grade") or "").strip().lower()
    if grade:
        fields["label_confidence_grade"] = (grade,)
    for field in _CORE_METADATA_FIELDS:
        value = _metadata_value(field, metadata.get(field))
        if value:
            fields[field] = (value,)
    if include_title:
        title = re.sub(
            r"[^0-9a-z\u4e00-\u9fff]+",
            "",
            str(metadata.get("title") or "").strip().lower(),
        )
        fields["title_length"] = (str(min(len(title) // 8, 8)),)
        bigrams = tuple(
            dict.fromkeys(title[index : index + 2] for index in range(len(title) - 1))
        )
        if bigrams:
            fields["title_bigrams"] = bigrams[:16]
    return fields


def fit_target_encoder(
    rows: Sequence[Mapping],
    *,
    protocol: str,
    config: TargetEncodingConfig | None = None,
) -> TargetEncodingModel:
    settings = config or TargetEncodingConfig()
    _validate_config(settings)
    if protocol not in {"account_time", "account_holdout"}:
        raise ValueError("target encoding protocol must be account_time or account_holdout")
    if not rows:
        raise ValueError("target encoding requires at least one training row")
    targets = [float(row["target"]) for row in rows]
    if not all(math.isfinite(target) for target in targets):
        raise ValueError("target encoding targets must be finite")
    global_mean = sum(targets) / len(targets)
    field_accumulator: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(lambda: [0.0, 0.0])
    )
    account_accumulator: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(lambda: [0.0, 0.0])
    )
    include_account_history = protocol == "account_time"
    for row, target in zip(rows, targets):
        fields = extract_target_encoding_fields(
            row,
            include_title=settings.include_title,
        )
        account_id = str(row.get("account_id") or "unknown")
        for field, values in fields.items():
            for value in values:
                _accumulate(field_accumulator[field][value], target)
                if include_account_history:
                    key = _account_value_key(account_id, value)
                    _accumulate(account_accumulator[field][key], target)
    return TargetEncodingModel(
        account_field_stats=_freeze_stats(account_accumulator),
        alpha=settings.alpha,
        field_stats=_freeze_stats(field_accumulator),
        global_mean=global_mean,
        include_account_history=include_account_history,
        include_title=settings.include_title,
        min_samples=settings.min_samples,
        protocol=protocol,
        training_sample_count=len(rows),
    )


def predict_target_encoding(
    model: TargetEncodingModel,
    row: Mapping,
) -> TargetEncodingPrediction:
    fields = extract_target_encoding_fields(row, include_title=model.include_title)
    account_id = str(row.get("account_id") or "unknown")
    field_scores: list[float] = []
    fallback_counts: dict[str, int] = defaultdict(int)
    for field, values in fields.items():
        value_scores: list[float] = []
        for value in values:
            score, source = _encode_value(model, field, value, account_id)
            value_scores.append(score)
            fallback_counts[source] += 1
        if value_scores:
            field_scores.append(sum(value_scores) / len(value_scores))
    score = (
        sum(field_scores) / len(field_scores)
        if field_scores
        else model.global_mean
    )
    if not field_scores:
        fallback_counts["global_mean"] += 1
    return TargetEncodingPrediction(
        fallback_counts=dict(sorted(fallback_counts.items())),
        feature_count=len(field_scores),
        score=round(score, 12),
    )


def cross_fit_target_encoder(
    rows: Sequence[Mapping],
    *,
    protocol: str,
    config: TargetEncodingConfig | None = None,
) -> dict[str, TargetEncodingPrediction]:
    settings = config or TargetEncodingConfig()
    _validate_config(settings)
    if len(rows) < 2:
        raise ValueError("cross-fit target encoding requires at least two rows")
    fold_rows: dict[int, list[Mapping]] = defaultdict(list)
    for row in rows:
        fold_rows[_fold_for_row(row, protocol=protocol, config=settings)].append(row)
    predictions: dict[str, TargetEncodingPrediction] = {}
    for fold, evaluation_rows in sorted(fold_rows.items()):
        training_rows = [
            row
            for other_fold, members in fold_rows.items()
            if other_fold != fold
            for row in members
        ]
        if not training_rows:
            raise ValueError("cross-fit fold leaves no target encoding training rows")
        model = fit_target_encoder(
            training_rows,
            protocol=protocol,
            config=settings,
        )
        for row in evaluation_rows:
            sample_id = str(row["sample_id"])
            predictions[sample_id] = predict_target_encoding(model, row)
    return predictions


def _encode_value(
    model: TargetEncodingModel,
    field: str,
    value: str,
    account_id: str,
) -> tuple[float, str]:
    global_stat = model.field_stats.get(field, {}).get(value)
    global_value = model.global_mean
    if global_stat and global_stat[0] >= model.min_samples:
        global_value = _smoothed_mean(
            global_stat,
            prior=model.global_mean,
            alpha=model.alpha,
        )
    if model.include_account_history:
        account_key = _account_value_key(account_id, value)
        account_stat = model.account_field_stats.get(field, {}).get(account_key)
        if account_stat and account_stat[0] >= model.min_samples:
            return (
                _smoothed_mean(
                    account_stat,
                    prior=global_value,
                    alpha=model.alpha,
                ),
                "account_value",
            )
    if global_stat and global_stat[0] >= model.min_samples:
        return global_value, "global_value"
    return model.global_mean, "global_mean"


def _fold_for_row(
    row: Mapping,
    *,
    protocol: str,
    config: TargetEncodingConfig,
) -> int:
    if protocol == "account_holdout":
        unit = str(row.get("account_id") or "unknown")
    elif protocol == "account_time":
        unit = str(row.get("source_group_id") or row["sample_id"])
    else:
        raise ValueError("target encoding protocol must be account_time or account_holdout")
    digest = blake2b(
        f"{config.seed}:{protocol}:{unit}".encode("utf-8"),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "big") % config.folds


def _validate_config(config: TargetEncodingConfig) -> None:
    if config.alpha < 0:
        raise ValueError("target encoding alpha must be non-negative")
    if config.min_samples < 1:
        raise ValueError("target encoding min_samples must be positive")
    if config.folds < 2:
        raise ValueError("target encoding folds must be at least two")


def _metadata_value(field: str, value: object) -> str:
    if value is None:
        return ""
    if field in _NUMERIC_BUCKET_FIELDS:
        if not isinstance(value, (int, float, str)) or isinstance(value, bool):
            return "unknown"
        if isinstance(value, str):
            normalized = value.strip().lower()
            if not normalized:
                return ""
            try:
                value = float(normalized)
            except ValueError:
                return normalized
        try:
            number = min(max(float(value), 0.0), 1.0)
        except (TypeError, ValueError):
            return "unknown"
        return str(min(int(number * 5), 4))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip().lower()


def _account_value_key(account_id: str, value: str) -> str:
    return f"{account_id}\x1f{value}"


def _accumulate(stat: list[float], target: float) -> None:
    stat[0] += 1.0
    stat[1] += target


def _freeze_stats(
    accumulator: Mapping[str, Mapping[str, Sequence[float]]],
) -> dict[str, dict[str, tuple[int, float]]]:
    return {
        field: {
            value: (int(stat[0]), float(stat[1]))
            for value, stat in sorted(values.items())
        }
        for field, values in sorted(accumulator.items())
    }


def _smoothed_mean(
    stat: tuple[int, float],
    *,
    prior: float,
    alpha: float,
) -> float:
    count, total = stat
    return (total + alpha * prior) / (count + alpha)


def _read_jsonl_index(
    path: Path,
    key: str,
    *,
    allowed_values: set[str] | None = None,
) -> dict[str, dict]:
    rows = {}
    key_pattern = re.compile(
        rf'"{re.escape(key)}"\s*:\s*("(?:\\.|[^"\\])*")'
    )
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            if allowed_values is not None:
                match = key_pattern.search(line)
                if match is None:
                    raise ValueError(f"missing {key} in {path.name}")
                value = str(json.loads(match.group(1)))
                if value not in allowed_values:
                    continue
            row = json.loads(line)
            value = str(row[key])
            if value in rows:
                raise ValueError(f"duplicate {key} in {path.name}: {value}")
            rows[value] = row
    return rows


def _load_safe_metadata(path: Path, expected_ids: set[str]) -> dict[str, dict]:
    database = Path(path).resolve()
    if not database.is_file():
        raise FileNotFoundError(f"target encoding database does not exist: {database}")
    columns = ("id", "title") + _CORE_METADATA_FIELDS
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            f"SELECT {', '.join(columns)} FROM historical_capture_samples"
        ).fetchall()
    finally:
        connection.close()
    return {
        str(row["id"]): {column: row[column] for column in columns if column != "id"}
        for row in rows
        if str(row["id"]) in expected_ids
    }


def _protocol_rows(
    protocol: str,
    *,
    labels: Mapping[str, Mapping],
    splits: Mapping[str, Mapping],
    metadata: Mapping[str, Mapping],
    allowed_splits: set[str],
) -> list[dict]:
    rows = []
    split_key = f"{protocol}_split"
    for sample_id in sorted(labels):
        split = splits[sample_id]
        split_name = str(split[split_key])
        if split_name not in allowed_splits:
            continue
        label = labels[sample_id]
        protocol_target = label.get("protocol_targets", {}).get(protocol, {})
        target = protocol_target.get("targets", {}).get("broad_heat")
        if target is None:
            continue
        rows.append(
            {
                "account_id": str(label["account_id"]),
                "confidence": protocol_target.get("confidence") or {},
                "duration_bucket": str(label["duration_bucket"]),
                "metadata": metadata[sample_id],
                "publication_age_bucket": str(label["publication_age_bucket"]),
                "published_at": str(split["published_at"]),
                "sample_id": sample_id,
                "source_group_id": str(split["source_group_id"]),
                "split": split_name,
                "target": float(target),
            }
        )
    return rows


def _prediction_payload(
    protocol: str,
    split_name: str,
    row: Mapping,
    prediction: TargetEncodingPrediction,
) -> dict:
    return {
        "account_id": str(row["account_id"]),
        "fallback_counts": dict(prediction.fallback_counts),
        "feature_count": prediction.feature_count,
        "model_version": INTERACTION_HEAT_TARGET_ENCODING_VERSION,
        "protocol": protocol,
        "sample_id": str(row["sample_id"]),
        "score": prediction.score,
        "split": split_name,
    }


def _model_payload(model: TargetEncodingModel) -> dict:
    return {
        "account_field_stats": _stats_payload(model.account_field_stats),
        "alpha": model.alpha,
        "field_stats": _stats_payload(model.field_stats),
        "global_mean": round(model.global_mean, 12),
        "include_account_history": model.include_account_history,
        "include_title": model.include_title,
        "min_samples": model.min_samples,
        "protocol": model.protocol,
        "training_sample_count": model.training_sample_count,
    }


def _stats_payload(
    stats: Mapping[str, Mapping[str, tuple[int, float]]],
) -> dict[str, dict[str, dict]]:
    return {
        field: {
            value: {
                "count": stat[0],
                "mean": round(stat[1] / stat[0], 12),
            }
            for value, stat in sorted(values.items())
        }
        for field, values in sorted(stats.items())
    }


def _ranking_metrics(rows: Sequence[Mapping], *, seed: int) -> dict:
    by_account: dict[str, list[Mapping]] = defaultdict(list)
    for row in rows:
        by_account[str(row["account_id"])].append(row)
    account_metrics = {}
    randomizer = random.Random(seed)
    for account_id, account_rows in sorted(by_account.items()):
        ranked = sorted(
            account_rows,
            key=lambda row: (-float(row["score"]), str(row["sample_id"])),
        )
        ideal = sorted(
            account_rows,
            key=lambda row: (-float(row["target"]), str(row["sample_id"])),
        )
        limit = min(10, len(account_rows))
        denominator = _dcg([float(row["target"]) for row in ideal[:limit]])
        ndcg = (
            _dcg([float(row["target"]) for row in ranked[:limit]]) / denominator
            if denominator
            else 0.0
        )
        random_ndcgs = []
        target_values = [float(row["target"]) for row in account_rows]
        for _ in range(30):
            shuffled = list(target_values)
            randomizer.shuffle(shuffled)
            random_ndcgs.append(
                _dcg(shuffled[:limit]) / denominator if denominator else 0.0
            )
        account_mean = sum(target_values) / len(target_values)
        top_mean = sum(float(row["target"]) for row in ranked[:limit]) / limit
        low_threshold = sorted(target_values)[int((len(target_values) - 1) * 0.25)]
        severe_count = sum(
            float(row["target"]) <= low_threshold for row in ranked[:limit]
        )
        account_metrics[account_id] = {
            "ndcg_at_10": round(ndcg, 6),
            "random_ndcg_at_10": round(sum(random_ndcgs) / len(random_ndcgs), 6),
            "sample_count": len(account_rows),
            "severe_misselection_rate": round(severe_count / limit, 6),
            "top10_heat_lift": round(top_mean - account_mean, 6),
        }
    metric_rows = list(account_metrics.values())
    return {
        "account_count": len(account_metrics),
        "account_metrics": account_metrics,
        "macro_ndcg_at_10": round(
            _mean([row["ndcg_at_10"] for row in metric_rows]), 6
        ),
        "macro_random_ndcg_at_10": round(
            _mean([row["random_ndcg_at_10"] for row in metric_rows]), 6
        ),
        "macro_severe_misselection_rate": round(
            _mean([row["severe_misselection_rate"] for row in metric_rows]), 6
        ),
        "macro_top10_heat_lift": round(
            _mean([row["top10_heat_lift"] for row in metric_rows]), 6
        ),
        "sample_count": len(rows),
    }


def _dcg(targets: Sequence[float]) -> float:
    return sum(
        ((2.0 ** max(0.0, min(1.0, target))) - 1.0) / math.log2(index + 2)
        for index, target in enumerate(targets)
    )


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _config_payload(config: TargetEncodingConfig) -> dict:
    return {
        "alpha": config.alpha,
        "folds": config.folds,
        "include_title": config.include_title,
        "min_samples": config.min_samples,
        "seed": config.seed,
    }


def _write_json(path: Path, payload: Mapping) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
