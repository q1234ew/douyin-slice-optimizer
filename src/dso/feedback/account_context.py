from __future__ import annotations

from typing import Any

from dso.db.session import connect, fetch_one


ACCOUNT_ROLES = {"unassigned", "publishing_target", "research_source"}
EVIDENCE_SCOPES = {"unclassified", "target_outcome", "research_proxy"}
TARGET_OUTCOME_SEMANTICS = "explicit_platform_outcome"
MIN_TARGET_OUTCOME_ITEMS = 30


def normalize_account_role(value: Any, *, default: str = "unassigned") -> str:
    role = str(value or default).strip().lower()
    if role not in ACCOUNT_ROLES:
        raise ValueError(f"account_role must be one of: {', '.join(sorted(ACCOUNT_ROLES))}")
    return role


def normalize_evidence_scope(value: Any, *, default: str = "unclassified") -> str:
    scope = str(value or default).strip().lower()
    if scope not in EVIDENCE_SCOPES:
        raise ValueError(f"evidence_scope must be one of: {', '.join(sorted(EVIDENCE_SCOPES))}")
    return scope


def default_evidence_scope(account_role: str) -> str:
    role = normalize_account_role(account_role)
    if role == "publishing_target":
        return "target_outcome"
    if role == "research_source":
        return "research_proxy"
    return "unclassified"


def platform_account_context(account_id: str = "main", platform: str = "douyin") -> dict[str, Any]:
    local_account_id = str(account_id or "main").strip() or "main"
    platform_name = str(platform or "douyin").strip().lower() or "douyin"
    with connect() as conn:
        account = fetch_one(
            conn,
            "SELECT * FROM platform_accounts WHERE platform = ? AND account_id = ?",
            [platform_name, local_account_id],
        )
        research = fetch_one(
            conn,
            "SELECT COUNT(*) AS sample_count FROM historical_capture_samples WHERE account_id = ?",
            [local_account_id],
        ) or {"sample_count": 0}
        mappings = fetch_one(
            conn,
            """
            SELECT COUNT(*) AS mapping_count,
                   SUM(CASE WHEN evidence_scope = 'target_outcome' THEN 1 ELSE 0 END) AS target_mapping_count,
                   SUM(CASE WHEN evidence_scope = 'research_proxy' THEN 1 ELSE 0 END) AS research_mapping_count,
                   SUM(CASE WHEN evidence_scope = 'unclassified' OR evidence_scope = '' THEN 1 ELSE 0 END) AS unclassified_mapping_count
            FROM platform_video_mappings
            WHERE platform = ? AND account_id = ?
            """,
            [platform_name, local_account_id],
        ) or {}
        metrics = fetch_one(
            conn,
            """
            SELECT COUNT(pm.id) AS metric_row_count,
                   COUNT(DISTINCT pm.platform_item_id) AS metric_item_count,
                   COUNT(DISTINCT CASE
                       WHEN m.evidence_scope = 'target_outcome'
                        AND pm.metric_semantics = ?
                        AND pm.sample_source != 'mock'
                        AND COALESCE(m.candidate_segment_id, '') != ''
                       THEN pm.platform_item_id END) AS verified_target_outcome_item_count,
                   COUNT(DISTINCT CASE
                       WHEN m.evidence_scope = 'target_outcome'
                        AND pm.metric_semantics = ?
                        AND pm.sample_source != 'mock'
                        AND (pm.views > 0 OR pm.impressions > 0)
                       THEN pm.platform_item_id END) AS traffic_item_count,
                   COUNT(DISTINCT CASE
                       WHEN m.evidence_scope = 'target_outcome'
                        AND pm.metric_semantics = ?
                        AND pm.sample_source != 'mock'
                        AND (pm.avg_watch_seconds > 0 OR pm.avg_watch_ratio > 0 OR pm.five_second_retention > 0
                             OR pm.completion_rate > 0 OR pm.rewatch_rate > 0)
                       THEN pm.platform_item_id END) AS watch_quality_item_count,
                   COUNT(DISTINCT CASE
                       WHEN m.evidence_scope = 'target_outcome'
                        AND pm.metric_semantics = ?
                        AND pm.sample_source != 'mock'
                        AND (pm.shares > 0 OR pm.follows > 0)
                       THEN pm.platform_item_id END) AS conversion_item_count,
                   SUM(CASE WHEN pm.metric_semantics = 'legacy_unverified' THEN 1 ELSE 0 END) AS legacy_unverified_row_count,
                   SUM(CASE WHEN pm.metric_semantics = 'ambiguous_visible_count' THEN 1 ELSE 0 END) AS ambiguous_visible_count_row_count
            FROM performance_metrics pm
            JOIN platform_video_mappings m
              ON m.platform = ? AND m.platform_item_id = pm.platform_item_id
            WHERE m.account_id = ?
            """,
            [
                TARGET_OUTCOME_SEMANTICS,
                TARGET_OUTCOME_SEMANTICS,
                TARGET_OUTCOME_SEMANTICS,
                TARGET_OUTCOME_SEMANTICS,
                platform_name,
                local_account_id,
            ],
        ) or {}
        orphan_metrics = fetch_one(
            conn,
            """
            SELECT COUNT(*) AS row_count
            FROM performance_metrics pm
            LEFT JOIN platform_video_mappings m ON m.platform_item_id = pm.platform_item_id
            WHERE m.id IS NULL
            """,
        ) or {"row_count": 0}

    stored_role = normalize_account_role((account or {}).get("account_role"))
    research_sample_count = int(research.get("sample_count") or 0)
    platform_account_id = str((account or {}).get("platform_account_id") or "").strip()
    if stored_role == "unassigned" and research_sample_count > 0 and not platform_account_id:
        role = "research_source"
        role_source = "derived_historical_samples"
    else:
        role = stored_role
        role_source = "platform_account" if account else "default"

    target_identity_ready = role == "publishing_target" and bool(platform_account_id)
    verified_items = int(metrics.get("verified_target_outcome_item_count") or 0)
    traffic_items = int(metrics.get("traffic_item_count") or 0)
    watch_items = int(metrics.get("watch_quality_item_count") or 0)
    conversion_items = int(metrics.get("conversion_item_count") or 0)
    blockers: list[str] = []
    if role != "publishing_target":
        blockers.append("publishing_target_not_designated")
    elif not target_identity_ready:
        blockers.append("platform_account_identity_missing")
    if verified_items <= 0:
        blockers.append("target_outcome_metrics_unavailable")
    elif verified_items < MIN_TARGET_OUTCOME_ITEMS:
        blockers.append("target_outcome_sample_insufficient")
    if verified_items > 0 and traffic_items <= 0:
        blockers.append("traffic_metric_missing")
    if verified_items > 0 and watch_items <= 0 and conversion_items <= 0:
        blockers.append("quality_and_conversion_metrics_missing")

    calibration_ready = (
        target_identity_ready
        and verified_items >= MIN_TARGET_OUTCOME_ITEMS
        and traffic_items > 0
        and (watch_items > 0 or conversion_items > 0)
    )
    if verified_items <= 0:
        outcome_status = "unavailable"
    elif calibration_ready:
        outcome_status = "calibration_ready"
    else:
        outcome_status = "insufficient"

    return {
        "contract_version": "platform_account_context.v1",
        "account_id": local_account_id,
        "platform": platform_name,
        "account_role": role,
        "account_role_source": role_source,
        "display_name": str((account or {}).get("display_name") or ""),
        "platform_account_id": platform_account_id,
        "auth_status": str((account or {}).get("auth_status") or "not_connected"),
        "is_publishing_target": role == "publishing_target",
        "is_research_source": role == "research_source",
        "research_sample_count": research_sample_count,
        "target_identity_ready": target_identity_ready,
        "target_outcome_status": outcome_status,
        "target_outcome_minimum_items": MIN_TARGET_OUTCOME_ITEMS,
        "cold_start": not calibration_ready,
        "production_personalization_allowed": calibration_ready,
        "research_prior_allowed": True,
        "blockers": blockers,
        "mapping_summary": {
            "count": int(mappings.get("mapping_count") or 0),
            "target_outcome": int(mappings.get("target_mapping_count") or 0),
            "research_proxy": int(mappings.get("research_mapping_count") or 0),
            "unclassified": int(mappings.get("unclassified_mapping_count") or 0),
        },
        "metric_summary": {
            "rows": int(metrics.get("metric_row_count") or 0),
            "items": int(metrics.get("metric_item_count") or 0),
            "verified_target_outcome_items": verified_items,
            "traffic_items": traffic_items,
            "watch_quality_items": watch_items,
            "conversion_items": conversion_items,
            "legacy_unverified_rows": int(metrics.get("legacy_unverified_row_count") or 0),
            "ambiguous_visible_count_rows": int(metrics.get("ambiguous_visible_count_row_count") or 0),
            "orphan_rows": int(orphan_metrics.get("row_count") or 0),
        },
        "evidence_policy": {
            "research_accounts": "cross_account_prior_only",
            "target_account": "cold_start_until_verified_outcomes",
            "legacy_metrics": "audit_only",
            "automatic_promotion": False,
        },
    }
