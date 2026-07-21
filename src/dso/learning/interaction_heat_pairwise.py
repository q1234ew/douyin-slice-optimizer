from __future__ import annotations

import math
import random
import re
import hashlib
import json
import shutil
import sqlite3
from hashlib import blake2b
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from dso.learning.interaction_heat_v3 import verify_interaction_heat_artifact
from dso.versions import INTERACTION_HEAT_PAIRWISE_VERSION


SparseFeatures = Mapping[int, float]
DEFAULT_PAIRWISE_EXPERIMENT_ID = "dso-interaction-heat-pairwise-20260720-r3"
_EXPERIMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,95}$")
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
_SAFE_METADATA_FIELDS = (
    "content_category",
    "hook_type",
    "slice_structure",
    "program_name",
    "artist_names",
    "song_title",
    "tags",
    "media_type",
    "commercial_intent",
    "rights_risk",
    "classification_confidence",
    "structure_confidence",
    "entity_signal",
    "is_original_sound",
)


@dataclass(frozen=True)
class PairwiseTrainingConfig:
    dimensions: int = 4096
    epochs: int = 5
    learning_rate: float = 0.05
    l2: float = 0.0001
    min_target_gap: float = 0.10
    max_pairs_per_sample: int = 4
    seed: int = 20260720
    feature_profile: str = "core"


@dataclass(frozen=True)
class PairwiseModel:
    weights: tuple[float, ...]
    pair_count: int


def run_local_pairwise_experiment(
    *,
    experiment_id: str = DEFAULT_PAIRWISE_EXPERIMENT_ID,
    label_artifact_dir: Path,
    expected_label_manifest_sha256: str,
    db_path: Path,
    output_root: Path,
    config: PairwiseTrainingConfig | None = None,
) -> dict:
    if not _EXPERIMENT_ID_RE.fullmatch(experiment_id):
        raise ValueError("invalid pairwise experiment_id")
    label_root = Path(label_artifact_dir).resolve()
    verification = verify_interaction_heat_artifact(
        label_root,
        expected_manifest_sha256=expected_label_manifest_sha256,
    )
    if not verification["passed"]:
        raise ValueError("interaction heat label artifact verification failed")
    settings = config or PairwiseTrainingConfig()
    labels = _read_jsonl_index(label_root / "labels.jsonl", "sample_id")
    splits = _read_jsonl_index(label_root / "splits.jsonl", "sample_id")
    if set(labels) != set(splits):
        raise ValueError("interaction heat labels and splits do not contain the same samples")
    metadata = _load_safe_metadata(Path(db_path), set(labels))
    missing_metadata = sorted(set(labels).difference(metadata))
    if missing_metadata:
        raise ValueError(
            f"historical metadata is missing {len(missing_metadata)} labeled samples"
        )

    models: dict[str, dict] = {}
    protocol_reports: dict[str, dict] = {}
    predictions: list[dict] = []
    for protocol in ("account_time", "account_holdout"):
        feature_rows = _protocol_feature_rows(
            protocol,
            labels=labels,
            splits=splits,
            metadata=metadata,
            config=settings,
        )
        training_rows = [row for row in feature_rows if row["split"] == "train"]
        model = fit_pairwise_logistic(training_rows, config=settings)
        models[protocol] = {
            "include_account_id": protocol == "account_time",
            "pair_count": model.pair_count,
            "nonzero_weight_count": sum(weight != 0.0 for weight in model.weights),
            "weights": [round(weight, 12) for weight in model.weights],
        }
        split_reports = {}
        for split_name in ("validation", "test"):
            evaluation_rows = [
                row for row in feature_rows if row["split"] == split_name
            ]
            scored = [
                dict(
                    row,
                    score=score_sparse_features(model.weights, row["features"]),
                )
                for row in evaluation_rows
            ]
            split_reports[split_name] = _ranking_metrics(
                scored,
                seed=settings.seed + (1 if split_name == "test" else 0),
            )
            predictions.extend(
                {
                    "protocol": protocol,
                    "sample_id": row["sample_id"],
                    "split": split_name,
                    "score": round(float(row["score"]), 12),
                }
                for row in scored
            )
        protocol_reports[protocol] = {
            "train_sample_count": len(training_rows),
            "pair_count": model.pair_count,
            "splits": split_reports,
        }

    target = Path(output_root).resolve() / experiment_id
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"pairwise experiment already exists: {target}")
    target.mkdir(mode=0o755)
    try:
        model_payload = {
            "config": _config_dict(settings),
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
                "feature_profile": settings.feature_profile,
                "hash": "blake2b-64 signed modulo dimensions",
                "safe_metadata_fields": list(
                    _CORE_METADATA_FIELDS
                    if settings.feature_profile == "core"
                    else _SAFE_METADATA_FIELDS
                ),
                "title_features": (
                    "disabled"
                    if settings.feature_profile == "core"
                    else "normalized length bucket + first 16 stable character bigrams"
                ),
            },
            "model_version": INTERACTION_HEAT_PAIRWISE_VERSION,
            "models": models,
        }
        report = {
            "admission": "research_only",
            "claim_limit": (
                "Offline ranking of observed interaction-heat proxies only; no claim "
                "about play traffic, exposure-normalized conversion, or production lift."
            ),
            "experiment_id": experiment_id,
            "protocols": protocol_reports,
            "status": "completed",
        }
        _write_json(target / "model.json", model_payload)
        _write_jsonl(
            target / "predictions.jsonl",
            sorted(
                predictions,
                key=lambda row: (row["protocol"], row["split"], row["sample_id"]),
            ),
        )
        _write_json(target / "report.json", report)
        files = {
            name: _sha256(target / name)
            for name in ("model.json", "predictions.jsonl", "report.json")
        }
        manifest = {
            "admission": "research_only",
            "config": _config_dict(settings),
            "contract_version": "interaction_heat_pairwise_experiment.v1",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "database_sha256": _sha256(Path(db_path)),
            "effective_model_cost_cny": "0.000000",
            "experiment_id": experiment_id,
            "files": files,
            "label_artifact_id": json.loads(
                (label_root / "manifest.json").read_text(encoding="utf-8")
            )["artifact_id"],
            "label_manifest_sha256": expected_label_manifest_sha256,
            "model_version": INTERACTION_HEAT_PAIRWISE_VERSION,
            "network_request_count": 0,
            "production_impact": {
                "automatic_export": False,
                "automatic_publish": False,
                "database_rows_updated": 0,
                "manual_gold_changed": False,
                "production_weight_changed": False,
            },
            "source_code_sha256": _sha256(Path(__file__)),
            "status": "completed",
        }
        manifest["manifest_sha256"] = _manifest_sha256(manifest)
        _write_json(target / "manifest.json", manifest)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return {
        "artifact_dir": str(target),
        "experiment_id": experiment_id,
        "manifest_sha256": manifest["manifest_sha256"],
        "network_request_count": 0,
        "production_weight_changed": False,
        "status": "completed",
    }


def hash_candidate_features(
    label: Mapping,
    split: Mapping,
    metadata: Mapping,
    *,
    dimensions: int,
    include_account_id: bool,
    feature_profile: str = "core",
) -> dict[int, float]:
    if dimensions < 1:
        raise ValueError("pairwise dimensions must be positive")
    if feature_profile not in {"core", "full"}:
        raise ValueError("pairwise feature_profile must be core or full")
    published = datetime.fromisoformat(
        str(split["published_at"]).replace("Z", "+00:00")
    )
    raw_confidence = label.get("confidence")
    confidence: Mapping = raw_confidence if isinstance(raw_confidence, Mapping) else {}
    fields: dict[str, list[str]] = {
        "duration_bucket": [str(label.get("duration_bucket") or "unknown")],
        "publication_age_bucket": [
            str(label.get("publication_age_bucket") or "unknown")
        ],
        "confidence_grade": [str(confidence.get("grade") or "unknown")],
        "published_month": [str(published.month)],
        "published_weekday": [str(published.weekday())],
        "published_hour_bucket": [str(published.hour // 4)],
    }
    metadata_fields = (
        _CORE_METADATA_FIELDS if feature_profile == "core" else _SAFE_METADATA_FIELDS
    )
    for field in metadata_fields:
        value = str(metadata.get(field) or "").strip().lower()
        if value:
            fields[field] = [value]
    if feature_profile == "full":
        title = re.sub(
            r"[^0-9a-z\u4e00-\u9fff]+",
            "",
            str(metadata.get("title") or "").strip().lower(),
        )
        fields["title_length"] = [str(min(len(title) // 8, 8))]
        title_bigrams = list(
            dict.fromkeys(title[index : index + 2] for index in range(len(title) - 1))
        )
        if title_bigrams:
            fields["title_bigrams"] = title_bigrams[:16]
    if include_account_id:
        fields["account_id"] = [str(label.get("account_id") or "unknown")]
    features: dict[int, float] = {}
    for field, values in fields.items():
        scale = 1.0 / math.sqrt(len(values))
        for value in values:
            digest = blake2b(f"{field}={value}".encode("utf-8"), digest_size=8).digest()
            encoded = int.from_bytes(digest, "big")
            index = encoded % dimensions
            sign = 1.0 if encoded & (1 << 63) else -1.0
            features[index] = features.get(index, 0.0) + sign * scale
    return {index: value for index, value in features.items() if value}


def fit_pairwise_logistic(
    rows: Sequence[dict],
    *,
    config: PairwiseTrainingConfig | None = None,
) -> PairwiseModel:
    settings = config or PairwiseTrainingConfig()
    if settings.dimensions < 1:
        raise ValueError("pairwise dimensions must be positive")
    pairs = _training_pairs(rows, settings)
    weights = [0.0] * settings.dimensions
    randomizer = random.Random(settings.seed)
    for _ in range(max(1, settings.epochs)):
        randomizer.shuffle(pairs)
        for better, worse in pairs:
            delta = _feature_delta(better["features"], worse["features"])
            margin = score_sparse_features(weights, delta)
            gradient = _negative_margin_probability(margin)
            for index, value in delta.items():
                weights[index] = (
                    weights[index] * (1.0 - settings.learning_rate * settings.l2)
                    + settings.learning_rate * gradient * value
                )
    return PairwiseModel(weights=tuple(weights), pair_count=len(pairs))


def score_sparse_features(weights: Sequence[float], features: SparseFeatures) -> float:
    return sum(weights[index] * value for index, value in features.items())


def _training_pairs(
    rows: Sequence[dict],
    config: PairwiseTrainingConfig,
) -> list[tuple[dict, dict]]:
    by_account: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_account[str(row["account_id"])].append(row)
    pairs: list[tuple[dict, dict]] = []
    for account_rows in by_account.values():
        ordered = sorted(
            account_rows,
            key=lambda row: (float(row["target"]), str(row["sample_id"])),
        )
        for lower_index, worse in enumerate(ordered):
            candidates = [
                better
                for better in reversed(ordered[lower_index + 1 :])
                if float(better["target"]) - float(worse["target"])
                >= config.min_target_gap
            ]
            for better in candidates[: max(1, config.max_pairs_per_sample)]:
                pairs.append((better, worse))
    return pairs


def _feature_delta(better: SparseFeatures, worse: SparseFeatures) -> dict[int, float]:
    delta = dict(better)
    for index, value in worse.items():
        delta[index] = delta.get(index, 0.0) - value
        if delta[index] == 0.0:
            del delta[index]
    return delta


def _negative_margin_probability(margin: float) -> float:
    if margin >= 0:
        exp_value = math.exp(-margin)
        return exp_value / (1.0 + exp_value)
    return 1.0 / (1.0 + math.exp(margin))


def _read_jsonl_index(path: Path, key: str) -> dict[str, dict]:
    rows = {}
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            rows[str(row[key])] = row
    return rows


def _load_safe_metadata(path: Path, sample_ids: set[str]) -> dict[str, dict]:
    database = Path(path).resolve()
    columns = ("id", "title", *_SAFE_METADATA_FIELDS)
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            f"SELECT {', '.join(columns)} FROM historical_capture_samples"
        ).fetchall()
    finally:
        connection.close()
    return {
        str(row["id"]): dict(row)
        for row in rows
        if str(row["id"]) in sample_ids
    }


def _protocol_feature_rows(
    protocol: str,
    *,
    labels: Mapping[str, dict],
    splits: Mapping[str, dict],
    metadata: Mapping[str, dict],
    config: PairwiseTrainingConfig,
) -> list[dict]:
    rows = []
    split_key = f"{protocol}_split"
    for sample_id in sorted(labels):
        split_name = str(splits[sample_id][split_key])
        if split_name not in {"train", "validation", "test"}:
            continue
        target = labels[sample_id]["protocol_targets"][protocol]["targets"][
            "broad_heat"
        ]
        if target is None:
            continue
        rows.append(
            {
                "account_id": str(labels[sample_id]["account_id"]),
                "features": hash_candidate_features(
                    labels[sample_id],
                    splits[sample_id],
                    metadata[sample_id],
                    dimensions=config.dimensions,
                    include_account_id=protocol == "account_time",
                    feature_profile=config.feature_profile,
                ),
                "sample_id": sample_id,
                "split": split_name,
                "target": float(target),
            }
        )
    return rows


def _ranking_metrics(rows: Sequence[dict], *, seed: int) -> dict:
    by_account: dict[str, list[dict]] = defaultdict(list)
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
            random_ndcgs.append(_dcg(shuffled[:limit]) / denominator if denominator else 0.0)
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


def _config_dict(config: PairwiseTrainingConfig) -> dict:
    return {
        "dimensions": config.dimensions,
        "epochs": config.epochs,
        "feature_profile": config.feature_profile,
        "l2": config.l2,
        "learning_rate": config.learning_rate,
        "max_pairs_per_sample": config.max_pairs_per_sample,
        "min_target_gap": config.min_target_gap,
        "seed": config.seed,
    }


def _write_json(path: Path, payload: Mapping) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: Sequence[Mapping]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                + "\n"
            )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_sha256(manifest: Mapping) -> str:
    payload = dict(manifest)
    payload.pop("manifest_sha256", None)
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
