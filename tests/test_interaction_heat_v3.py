from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dso.learning.interaction_heat_v3 as interaction_heat_v3
from dso.learning.interaction_heat_v3 import (
    INTERACTION_HEAT_LABEL_VERSION,
    build_interaction_heat_dataset,
    export_interaction_heat_input_snapshot,
    freeze_interaction_heat_artifact,
    freeze_interaction_heat_from_snapshot,
    load_interaction_heat_rows,
    verify_interaction_heat_artifact,
)


METRIC_SOURCES = {
    "likes": "digg_count",
    "comments": "comment_count",
    "favorites": "collect_count",
    "shares": "share_count",
}

TITLE_WORDS = (
    "amber", "birch", "coral", "dawn", "ember", "frost",
    "grove", "harbor", "iris", "jade", "kestrel", "lilac",
)


def _row(account: str, index: int, *, title: str | None = None) -> dict:
    day = 1 + index
    return {
        "id": f"{account}-{index:02d}",
        "account_id": account,
        "dataset_id": f"{account}_20260630_appleevents_api",
        "program_key": account,
        "program_name": "music-show" if index % 4 == 0 else "",
        "platform": "douyin",
        "platform_item_id": f"{account}-item-{index:02d}",
        "sample_key": f"sample-{account}-{index:02d}",
        "title": title or f"{account} unique performance {TITLE_WORDS[index]}",
        "published_at": f"2026-05-{day:02d}T08:00:00+00:00",
        "observed_at": "2026-06-30T12:00:00+00:00",
        "duration_seconds": 12.0 + index * 2.0,
        "likes": 10 + index * 4,
        "comments": index,
        "favorites": 2 + index,
        "shares": index // 2,
        "metric_sources": dict(METRIC_SOURCES),
    }


def _rows() -> list[dict]:
    return [
        _row(f"account-{letter}", index)
        for letter in "abcdef"
        for index in range(12)
    ]


class InteractionHeatV3Test(unittest.TestCase):
    def test_labels_preserve_zero_and_missing_semantics(self) -> None:
        rows = _rows()
        rows[0]["comments"] = 0
        rows[0]["shares"] = 0
        rows[1]["comments"] = 0
        rows[1]["metric_sources"].pop("comments")

        dataset = build_interaction_heat_dataset(rows, min_group_samples=3)
        by_id = {item["sample_id"]: item for item in dataset["labels"]}
        first = by_id[rows[0]["id"]]
        second = by_id[rows[1]["id"]]

        self.assertEqual(dataset["label_version"], INTERACTION_HEAT_LABEL_VERSION)
        self.assertEqual(
            set(first["targets"]),
            {"like_heat", "discussion_heat", "favorite_heat", "share_heat", "broad_heat"},
        )
        self.assertIsNotNone(first["targets"]["discussion_heat"])
        self.assertIsNotNone(first["targets"]["share_heat"])
        self.assertFalse(first["metric_missing"]["comments"])
        self.assertTrue(second["metric_missing"]["comments"])
        self.assertIsNone(second["targets"]["discussion_heat"])
        self.assertGreaterEqual(first["targets"]["broad_heat"], 0.0)
        self.assertLessEqual(first["targets"]["broad_heat"], 1.0)
        self.assertGreater(first["normalization_sample_counts"]["discussion_heat"], 0)

    def test_provenance_does_not_turn_empty_metric_values_into_zero(self) -> None:
        rows = _rows()
        rows[0]["comments"] = None
        rows[1]["shares"] = ""

        dataset = build_interaction_heat_dataset(rows, min_group_samples=3)
        by_id = {item["sample_id"]: item for item in dataset["labels"]}

        self.assertTrue(by_id[rows[0]["id"]]["metric_missing"]["comments"])
        self.assertIsNone(by_id[rows[0]["id"]]["targets"]["discussion_heat"])
        self.assertTrue(by_id[rows[1]["id"]]["metric_missing"]["shares"])
        self.assertIsNone(by_id[rows[1]["id"]]["targets"]["share_heat"])

    def test_sqlite_loader_keeps_valid_all_zero_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "samples.sqlite3"
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE historical_capture_samples (
                        id TEXT, account_id TEXT, dataset_id TEXT, program_key TEXT,
                        program_name TEXT, song_title TEXT, platform TEXT,
                        platform_item_id TEXT, sample_key TEXT, title TEXT,
                        published_at TEXT, collected_at TEXT, duration_seconds REAL,
                        likes INTEGER, comments INTEGER, favorites INTEGER, shares INTEGER,
                        reward_proxy REAL, raw_json TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO historical_capture_samples VALUES (
                        'zero-1', 'account-zero', 'dataset-zero', 'program-zero',
                        '', '', 'douyin', 'item-zero', 'sample-zero', 'valid zero',
                        '2026-05-01T00:00:00+00:00', '2026-06-01T00:00:00+00:00',
                        10.0, 0, 0, 0, 0, 0.0, ?
                    )
                    """,
                    (json.dumps({"clean": {"metric_sources": METRIC_SOURCES}}),),
                )
                connection.execute(
                    """
                    INSERT INTO historical_capture_samples VALUES (
                        'missing-1', 'account-zero', 'dataset-zero', 'program-zero',
                        '', '', 'douyin', 'item-missing', 'sample-missing', 'missing outcomes',
                        '2026-05-02T00:00:00+00:00', '2026-06-01T00:00:00+00:00',
                        10.0, 0, 0, 0, 0, 0.0, '{}'
                    )
                    """
                )

            rows = load_interaction_heat_rows(db_path)

        self.assertEqual([row["id"] for row in rows], ["zero-1"])
        self.assertEqual(rows[0]["likes"], 0)

    def test_time_holdout_does_not_fit_normalizers(self) -> None:
        rows = _rows()
        first = build_interaction_heat_dataset(rows, min_group_samples=3)
        time_test_id = next(
            item["sample_id"]
            for item in first["splits"]
            if item["account_time_split"] == "test"
        )
        changed = copy.deepcopy(rows)
        changed_row = next(item for item in changed if item["id"] == time_test_id)
        changed_row["likes"] = 99_999_999
        changed_row["comments"] = 99_999

        second = build_interaction_heat_dataset(changed, min_group_samples=3)

        self.assertEqual(first["normalizers"]["account_time"], second["normalizers"]["account_time"])
        first_labels = {item["sample_id"]: item for item in first["labels"]}
        second_labels = {item["sample_id"]: item for item in second["labels"]}
        unaffected_id = next(
            item["sample_id"]
            for item in first["splits"]
            if item["account_time_split"] == "train" and item["sample_id"] != time_test_id
        )
        self.assertEqual(first_labels[unaffected_id], second_labels[unaffected_id])

    def test_account_holdout_uses_no_held_account_statistics(self) -> None:
        dataset = build_interaction_heat_dataset(_rows(), min_group_samples=3)
        split_by_id = {item["sample_id"]: item for item in dataset["splits"]}
        held_accounts = set(dataset["split_summary"]["account_holdout"]["accounts"]["validation"])
        held_accounts.update(dataset["split_summary"]["account_holdout"]["accounts"]["test"])
        self.assertGreaterEqual(len(held_accounts), 2)
        for item in dataset["splits"]:
            if item["account_id"] in held_accounts:
                self.assertIn(
                    item["account_holdout_split"],
                    {"validation", "test", "excluded_leakage"},
                )
        normalizer_keys = {
            key
            for metric in dataset["normalizers"]["account_holdout"]["metrics"].values()
            for key in metric
        }
        for account in held_accounts:
            self.assertFalse(any(f"account={account}|" in key or key == f"account={account}" for key in normalizer_keys))
        self.assertEqual(len(split_by_id), len(dataset["labels"]))

    def test_title_and_media_source_groups_never_cross_partitions(self) -> None:
        rows = _rows()
        rows[5]["title"] = "同一舞台高光！第1版"
        rows[20]["title"] = "同一舞台高光 第2版"
        media = {
            rows[7]["platform_item_id"]: ["same-media-sha"],
            rows[31]["platform_item_id"]: ["same-media-sha"],
        }

        dataset = build_interaction_heat_dataset(rows, media_sha_by_item=media, min_group_samples=3)

        for split_field in ("account_time_split", "account_holdout_split"):
            source_splits: dict[str, set[str]] = {}
            for item in dataset["splits"]:
                if item[split_field] == "excluded_leakage":
                    continue
                source_splits.setdefault(item["source_group_id"], set()).add(item[split_field])
            self.assertTrue(all(len(values) == 1 for values in source_splits.values()))
        self.assertEqual(dataset["leakage_audit"]["account_time"]["cross_split_source_group_count"], 0)
        self.assertEqual(dataset["leakage_audit"]["account_holdout"]["cross_split_source_group_count"], 0)

    def test_freeze_is_immutable_and_reproducible(self) -> None:
        rows = _rows()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = freeze_interaction_heat_artifact(
                artifact_id="interaction-heat-test-r1",
                rows=rows,
                output_root=root / "a",
                created_at="2026-07-20T00:00:00+00:00",
                min_group_samples=3,
            )
            input_path = root / "input.jsonl"
            input_path.write_text(
                "".join(
                    json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                    for row in rows
                ),
                encoding="utf-8",
            )
            media_path = root / "media.json"
            media_path.write_text("{}\n", encoding="utf-8")
            second = freeze_interaction_heat_from_snapshot(
                artifact_id="interaction-heat-test-r1-copy",
                input_path=input_path,
                media_index_path=media_path,
                output_root=root / "b",
                created_at="2026-07-20T00:00:00+00:00",
                min_group_samples=3,
            )

            self.assertEqual(first["labels_sha256"], second["labels_sha256"])
            self.assertEqual(first["splits_sha256"], second["splits_sha256"])
            artifact_dir = Path(first["artifact_dir"])
            self.assertTrue((artifact_dir / "manifest.json").is_file())
            self.assertTrue((artifact_dir / "labels.jsonl").is_file())
            self.assertTrue((artifact_dir / "splits.jsonl").is_file())
            self.assertTrue((artifact_dir / "normalizers.json").is_file())
            self.assertTrue((artifact_dir / "report.json").is_file())
            manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["network_request_count"], 0)
            self.assertEqual(manifest["effective_model_cost_cny"], "0.000000")
            self.assertFalse(verify_interaction_heat_artifact(artifact_dir)["passed"])
            self.assertTrue(
                verify_interaction_heat_artifact(
                    artifact_dir,
                    expected_manifest_sha256=first["manifest_sha256"],
                )["passed"]
            )
            with self.assertRaises(FileExistsError):
                freeze_interaction_heat_artifact(
                    artifact_id="interaction-heat-test-r1",
                    rows=rows,
                    output_root=root / "a",
                    created_at="2026-07-20T00:00:00+00:00",
                    min_group_samples=3,
                )

    def test_verify_rejects_path_escape_missing_files_and_rewritten_manifest(self) -> None:
        rows = _rows()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            frozen = freeze_interaction_heat_artifact(
                artifact_id="interaction-heat-verify-r1",
                rows=rows,
                output_root=root,
                created_at="2026-07-20T00:00:00+00:00",
                min_group_samples=3,
            )
            artifact_dir = Path(frozen["artifact_dir"])
            manifest_path = artifact_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            outside = root / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            manifest["files"][str(outside)] = hashlib.sha256(b"outside").hexdigest()
            manifest["files"].pop("labels.jsonl")
            manifest["manifest_sha256"] = _test_manifest_sha256(manifest)
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            result = verify_interaction_heat_artifact(
                artifact_dir,
                expected_manifest_sha256=frozen["manifest_sha256"],
            )

        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["trusted_manifest_sha256"])
        self.assertFalse(result["checks"]["expected_files"])
        self.assertNotIn(str(outside), result["file_checks"])

    def test_verify_rejects_payload_symlink_outside_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            frozen = freeze_interaction_heat_artifact(
                artifact_id="interaction-heat-symlink-r1",
                rows=_rows(),
                output_root=root,
                created_at="2026-07-20T00:00:00+00:00",
                min_group_samples=3,
            )
            artifact_dir = Path(frozen["artifact_dir"])
            labels = artifact_dir / "labels.jsonl"
            outside = root / "outside-labels.jsonl"
            outside.write_bytes(labels.read_bytes())
            labels.unlink()
            labels.symlink_to(outside)

            result = verify_interaction_heat_artifact(
                artifact_dir,
                expected_manifest_sha256=frozen["manifest_sha256"],
            )

        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["safe_file_paths"])
        self.assertFalse(result["file_checks"]["labels.jsonl"])

    def test_verify_rejects_internal_payload_symlink_and_extra_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            frozen = freeze_interaction_heat_artifact(
                artifact_id="interaction-heat-internal-symlink-r1",
                rows=_rows(),
                output_root=root,
                created_at="2026-07-20T00:00:00+00:00",
                min_group_samples=3,
            )
            artifact_dir = Path(frozen["artifact_dir"])
            labels = artifact_dir / "labels.jsonl"
            alias = artifact_dir / "labels-copy.jsonl"
            alias.write_bytes(labels.read_bytes())
            labels.unlink()
            labels.symlink_to(alias.name)

            result = verify_interaction_heat_artifact(
                artifact_dir,
                expected_manifest_sha256=frozen["manifest_sha256"],
            )

        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["safe_file_paths"])
        self.assertFalse(result["checks"]["exact_directory_entries"])
        self.assertFalse(result["file_checks"]["labels.jsonl"])

    def test_verify_rejects_an_extra_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            frozen = freeze_interaction_heat_artifact(
                artifact_id="interaction-heat-extra-file-r1",
                rows=_rows(),
                output_root=root,
                created_at="2026-07-20T00:00:00+00:00",
                min_group_samples=3,
            )
            artifact_dir = Path(frozen["artifact_dir"])
            (artifact_dir / "unexpected.txt").write_text("unexpected", encoding="utf-8")

            result = verify_interaction_heat_artifact(
                artifact_dir,
                expected_manifest_sha256=frozen["manifest_sha256"],
            )

        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["exact_directory_entries"])

    def test_snapshot_export_rejects_path_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "snapshot.json"
            with self.assertRaisesRegex(ValueError, "different paths"):
                export_interaction_heat_input_snapshot(
                    db_path=Path(temp_dir) / "missing.sqlite3",
                    input_path=target,
                    media_index_path=target,
                    repo_root=Path(temp_dir),
                )

    def test_snapshot_export_rejects_dangling_output_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target.jsonl"
            dangling = root / "input.jsonl"
            dangling.symlink_to(target)
            media_path = root / "media.json"

            with self.assertRaises(FileExistsError):
                export_interaction_heat_input_snapshot(
                    db_path=root / "unused.sqlite3",
                    input_path=dangling,
                    media_index_path=media_path,
                    repo_root=root,
                )

            self.assertTrue(dangling.is_symlink())
            self.assertFalse(target.exists())
            self.assertFalse(media_path.exists())

    def test_snapshot_export_rolls_back_when_second_publish_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "input.jsonl"
            media_path = root / "media.json"
            real_link = interaction_heat_v3.os.link
            link_count = 0

            def fail_second_link(source: Path, target: Path) -> None:
                nonlocal link_count
                link_count += 1
                if link_count == 2:
                    raise OSError("synthetic second publish failure")
                real_link(source, target)

            with (
                mock.patch.object(
                    interaction_heat_v3,
                    "load_interaction_heat_rows",
                    return_value=_rows(),
                ),
                mock.patch.object(
                    interaction_heat_v3,
                    "_local_media_sha_index",
                    return_value=({}, {"media_file_count": 0}),
                ),
                mock.patch.object(
                    interaction_heat_v3.os,
                    "link",
                    side_effect=fail_second_link,
                ),
                self.assertRaisesRegex(OSError, "second publish failure"),
            ):
                export_interaction_heat_input_snapshot(
                    db_path=root / "unused.sqlite3",
                    input_path=input_path,
                    media_index_path=media_path,
                    repo_root=root,
                )

            self.assertFalse(input_path.exists())
            self.assertFalse(media_path.exists())
            self.assertEqual(list(root.glob(".*-*")), [])

    def test_snapshot_rollback_preserves_concurrent_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "input.jsonl"
            media_path = root / "media.json"
            real_link = interaction_heat_v3.os.link
            link_count = 0

            def replace_before_second_failure(source: Path, target: Path) -> None:
                nonlocal link_count
                link_count += 1
                if link_count == 2:
                    input_path.unlink()
                    input_path.write_text("concurrent replacement", encoding="utf-8")
                    raise OSError("synthetic raced publish failure")
                real_link(source, target)

            with (
                mock.patch.object(
                    interaction_heat_v3,
                    "load_interaction_heat_rows",
                    return_value=_rows(),
                ),
                mock.patch.object(
                    interaction_heat_v3,
                    "_local_media_sha_index",
                    return_value=({}, {"media_file_count": 0}),
                ),
                mock.patch.object(
                    interaction_heat_v3.os,
                    "link",
                    side_effect=replace_before_second_failure,
                ),
                self.assertRaisesRegex(OSError, "raced publish failure"),
            ):
                export_interaction_heat_input_snapshot(
                    db_path=root / "unused.sqlite3",
                    input_path=input_path,
                    media_index_path=media_path,
                    repo_root=root,
                )

            self.assertEqual(input_path.read_text(encoding="utf-8"), "concurrent replacement")
            self.assertFalse(media_path.exists())

    def test_snapshot_freeze_rejects_canonical_metadata_overrides(self) -> None:
        rows = _rows()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "input.jsonl"
            input_path.write_text(
                "".join(
                    json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                    for row in rows
                ),
                encoding="utf-8",
            )
            media_path = root / "media.json"
            media_path.write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "canonical source metadata"):
                freeze_interaction_heat_from_snapshot(
                    artifact_id="interaction-heat-protected-metadata-r1",
                    input_path=input_path,
                    media_index_path=media_path,
                    output_root=root / "artifacts",
                    min_group_samples=3,
                    source_metadata_overrides={"input_snapshot_sha256": "forged"},
                )

            with self.assertRaisesRegex(ValueError, "canonical source metadata"):
                freeze_interaction_heat_from_snapshot(
                    artifact_id="interaction-heat-protected-git-r1",
                    input_path=input_path,
                    media_index_path=media_path,
                    output_root=root / "git-artifacts",
                    min_group_samples=3,
                    source_metadata_overrides={
                        "git_parent_commit": "forged",
                        "git_branch": "forged",
                    },
                )


def _test_manifest_sha256(manifest: dict) -> str:
    payload = dict(manifest)
    payload.pop("manifest_sha256", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


if __name__ == "__main__":
    unittest.main()
