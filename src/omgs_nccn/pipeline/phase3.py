from __future__ import annotations

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from omgs_nccn.config.paths import REPO_ROOT
from omgs_nccn.config.paths import ov_2025_roots


CANONICAL_TOP_LEVEL_FIELDS = [
    "graph_id",
    "guideline",
    "version",
    "graph_type",
    "status",
    "page_order",
    "nodes",
    "edges",
    "source_pages",
]

CANONICAL_NODE_FIELDS = [
    "id",
    "page_code",
    "page_number",
    "local_node_id",
    "node_type",
    "node_label",
    "verbatim_text",
    "text_snippet",
    "page_local_bbox",
    "global_bbox",
    "guideline_header",
    "page_title",
    "page_scope_summary",
    "explicit_ref_ids",
    "explicit_ref_count",
    "has_explicit_refs",
    "reviewed_footnote_ids",
    "reviewed_footnote_labels",
    "reviewed_footnote_texts",
    "reviewed_footnote_count",
    "has_reviewed_footnotes",
    "reviewed_footnote_ref_ids",
    "reviewed_footnote_ref_count",
    "has_reviewed_footnote_refs",
    "is_uncertain",
]

CANONICAL_EDGE_FIELDS = [
    "id",
    "source_node_id",
    "target_node_id",
    "source_page_code",
    "target_page_code",
    "edge_type",
    "edge_label",
    "stitch_kind",
    "local_edge_id",
    "is_uncertain",
]

AUDIT_NODE_FIELDS = [
    "why_node",
    "source_shape_id",
    "origin",
]

AUDIT_EDGE_FIELDS = [
    "why_edge",
    "source_shape_id",
]

VALID_NODE_TYPES = {"process", "stage", "decision", "reference", "cross_page"}
VALID_EDGE_TYPES = {"flow", "cross_page_ref"}
VALID_STITCH_KINDS = {"intra_page", "cross_page", "external_ref"}
VALID_NODE_LABELS = {"Disease Condition", "Evaluation", "Treatment Option", "Page Jump"}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _validate_bbox(node_id: str, bbox: Any, *, allow_null: bool) -> str | None:
    if bbox is None:
        if allow_null:
            return None
        return f"missing_bbox:{node_id}"
    if not isinstance(bbox, list) or len(bbox) != 4:
        return f"invalid_bbox:{node_id}:{bbox}"
    if bbox[2] <= 0 or bbox[3] <= 0:
        return f"invalid_bbox:{node_id}:{bbox}"
    return None


def _validate_reviewed_global_graph(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required_top = {
        "graph_id",
        "guideline",
        "version",
        "graph_type",
        "status",
        "page_order",
        "nodes",
        "edges",
        "layout",
        "source_pages",
    }
    missing_top = sorted(required_top.difference(payload.keys()))
    if missing_top:
        errors.append(f"missing_top_level_fields:{missing_top}")

    if payload.get("graph_type") != "directed":
        errors.append(f"graph_type_must_be_directed:{payload.get('graph_type')}")

    node_ids: list[str] = []
    for node in payload.get("nodes", []):
        node_id = node.get("id")
        node_ids.append(node_id)
        if not node.get("verbatim_text", "").strip():
            errors.append(f"empty_node_text:{node_id}")
        node_type = node.get("node_type")
        if node_type not in VALID_NODE_TYPES:
            errors.append(f"invalid_node_type:{node_id}:{node_type}")
        node_label = node.get("node_label")
        if node_label is not None and node_label not in VALID_NODE_LABELS:
            errors.append(f"invalid_node_label:{node_id}:{node_label}")
        allow_null_bbox = node.get("page_code") == "EXTERNAL"
        page_local_err = _validate_bbox(node_id, node.get("page_local_bbox"), allow_null=allow_null_bbox)
        if page_local_err:
            errors.append(page_local_err)
        global_err = _validate_bbox(node_id, node.get("global_bbox"), allow_null=allow_null_bbox)
        if global_err:
            errors.append(global_err)

    if len(node_ids) != len(set(node_ids)):
        errors.append("duplicate_node_ids")

    node_id_set = set(node_ids)
    edge_ids: list[str] = []
    edge_pairs: set[tuple[str, str]] = set()
    for edge in payload.get("edges", []):
        edge_id = edge.get("id")
        edge_ids.append(edge_id)
        edge_type = edge.get("edge_type")
        if edge_type not in VALID_EDGE_TYPES:
            errors.append(f"invalid_edge_type:{edge_id}:{edge_type}")
        stitch_kind = edge.get("stitch_kind")
        if stitch_kind not in VALID_STITCH_KINDS:
            errors.append(f"invalid_stitch_kind:{edge_id}:{stitch_kind}")
        source_node_id = edge.get("source_node_id")
        target_node_id = edge.get("target_node_id")
        if not source_node_id or source_node_id not in node_id_set:
            errors.append(f"edge_source_missing:{edge_id}:{source_node_id}")
        if not target_node_id or target_node_id not in node_id_set:
            errors.append(f"edge_target_missing:{edge_id}:{target_node_id}")
        if isinstance(source_node_id, str) and source_node_id.startswith("shape:"):
            errors.append(f"edge_source_shape_id_leak:{edge_id}:{source_node_id}")
        if isinstance(target_node_id, str) and target_node_id.startswith("shape:"):
            errors.append(f"edge_target_shape_id_leak:{edge_id}:{target_node_id}")
        pair = (str(source_node_id), str(target_node_id))
        if pair in edge_pairs:
            errors.append(f"duplicate_directed_pair:{pair[0]}->{pair[1]}")
        edge_pairs.add(pair)

    if len(edge_ids) != len(set(edge_ids)):
        errors.append("duplicate_edge_ids")

    return errors


def _normalize_node(node: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    canonical = {field: node.get(field) for field in CANONICAL_NODE_FIELDS}
    canonical["is_uncertain"] = bool(canonical.get("is_uncertain", False))
    audit = {
        "id": node["id"],
        "why_node": node.get("why_node", ""),
        "local_node_shape_id": node.get("source_shape_id"),
        "source_origin": node.get("origin"),
    }
    return canonical, audit


def _normalize_edge(edge: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    canonical = {field: edge.get(field) for field in CANONICAL_EDGE_FIELDS}
    canonical["is_uncertain"] = bool(canonical.get("is_uncertain", False))
    audit = {
        "id": edge["id"],
        "why_edge": edge.get("why_edge", ""),
        "local_edge_shape_id": edge.get("source_shape_id"),
    }
    return canonical, audit


def build_rule_graph(
    *,
    reviewed_graph_path: Path | None = None,
    rule_graph_root: Path | None = None,
    report_root: Path | None = None,
) -> dict[str, Any]:
    roots = ov_2025_roots()
    reviewed_graph_path = reviewed_graph_path or (roots["reviewed_graph"] / "ov_2025_global.reviewed_graph.json")
    rule_graph_root = rule_graph_root or (roots["processed_root"] / "rule_graph")
    report_root = report_root or roots["reports"]

    payload = _read_json(reviewed_graph_path)
    errors = _validate_reviewed_global_graph(payload)
    report_path = report_root / "ov_2025_rule_graph_normalization_report.json"

    if errors:
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "error",
            "reviewed_graph_path": str(reviewed_graph_path),
            "blocking_error_count": len(errors),
            "blocking_errors": errors,
        }
        _write_json(report_path, report)
        return {
            "status": "error",
            "stage": "phase3_validation",
            "reviewed_graph_path": str(reviewed_graph_path),
            "report_path": str(report_path),
            "blocking_error_count": len(errors),
        }

    canonical_nodes: list[dict[str, Any]] = []
    audit_nodes: list[dict[str, Any]] = []
    for node in payload["nodes"]:
        canonical, audit = _normalize_node(node)
        canonical_nodes.append(canonical)
        audit_nodes.append(audit)

    canonical_edges: list[dict[str, Any]] = []
    audit_edges: list[dict[str, Any]] = []
    for edge in payload["edges"]:
        canonical, audit = _normalize_edge(edge)
        canonical_edges.append(canonical)
        audit_edges.append(audit)

    canonical_graph = {
        "graph_id": payload["graph_id"],
        "guideline": payload["guideline"],
        "version": payload["version"],
        "graph_type": payload["graph_type"],
        "status": "canonical_rule_graph",
        "page_order": payload["page_order"],
        "nodes": canonical_nodes,
        "edges": canonical_edges,
        "source_pages": payload["source_pages"],
    }

    audit_payload = {
        "graph_id": payload["graph_id"],
        "guideline": payload["guideline"],
        "version": payload["version"],
        "source_reviewed_graph": str(reviewed_graph_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "moved_top_level_fields": {
            "layout": payload.get("layout"),
        },
        "moved_node_fields": audit_nodes,
        "moved_edge_fields": audit_edges,
        "field_policy": {
            "kept_top_level_fields": CANONICAL_TOP_LEVEL_FIELDS,
            "kept_node_fields": CANONICAL_NODE_FIELDS,
            "kept_edge_fields": CANONICAL_EDGE_FIELDS,
            "moved_node_fields": ["why_node", "source_shape_id", "origin"],
            "moved_edge_fields": ["why_edge", "source_shape_id"],
            "dropped_fields": [],
        },
    }

    rule_graph_path = rule_graph_root / "ov_2025_global.rule_graph.json"
    audit_path = rule_graph_root / "ov_2025_global.rule_graph.audit.json"
    _write_json(rule_graph_path, canonical_graph)
    _write_json(audit_path, audit_payload)

    normalization_report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
        "reviewed_graph_path": str(reviewed_graph_path),
        "rule_graph_path": str(rule_graph_path),
        "audit_path": str(audit_path),
        "kept_top_level_fields": CANONICAL_TOP_LEVEL_FIELDS,
        "kept_node_fields": CANONICAL_NODE_FIELDS,
        "kept_edge_fields": CANONICAL_EDGE_FIELDS,
        "moved_node_fields": ["why_node", "source_shape_id", "origin"],
        "moved_edge_fields": ["why_edge", "source_shape_id"],
        "moved_top_level_fields": ["layout"],
        "dropped_fields": [],
        "node_count": len(canonical_nodes),
        "edge_count": len(canonical_edges),
        "cross_page_edge_count": sum(1 for edge in canonical_edges if edge["stitch_kind"] == "cross_page"),
        "external_ref_edge_count": sum(1 for edge in canonical_edges if edge["stitch_kind"] == "external_ref"),
        "external_alias_node_count": sum(1 for node in canonical_nodes if node["page_code"] == "EXTERNAL"),
        "blocking_violation_count": 0,
    }
    _write_json(report_path, normalization_report)

    return {
        "status": "ok",
        "reviewed_graph_path": str(reviewed_graph_path),
        "rule_graph_path": str(rule_graph_path),
        "audit_path": str(audit_path),
        "report_path": str(report_path),
        "node_count": len(canonical_nodes),
        "edge_count": len(canonical_edges),
    }
