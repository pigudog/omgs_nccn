from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import date
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from omgs_nccn.config.paths import REPO_ROOT
from omgs_nccn.config.paths import ov_2025_roots


PAGE_REF_RE = re.compile(r"\b(OV-\d+|LCOC-\d+)\b")
ALIAS_REF_RE = re.compile(r"\b(OV-[A-Z]|LCOC-[A-Z])\b")
RANGE_REF_RE = re.compile(r"\b(OV|LCOC)-(\d+)\s+to\s+(OV|LCOC)-(\d+)\b")
CROSS_PAGE_TEXT_HINT_RE = re.compile(r"\b(OV-\d+|LCOC-\d+|OV-[A-Z]|LCOC-[A-Z])\b")
VALID_NODE_TYPES = {"process", "stage", "decision", "reference", "cross_page"}
VALID_EDGE_TYPES = {"flow", "cross_page_ref"}
VALID_NODE_LABELS = {"Disease Condition", "Evaluation", "Treatment Option", "Page Jump"}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _load_optional_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    return _read_json(path)


def _page_dir(page_code: str, input_root: Path) -> Path:
    return input_root / page_code


def _page_graph_path(page_code: str, input_root: Path, page_filename: str) -> Path:
    return _page_dir(page_code, input_root) / page_filename


def _load_stitch_map(stitch_map_path: Path) -> dict[str, Any]:
    return _read_json(stitch_map_path)


def _page_size_from_nodes(nodes: list[dict[str, Any]]) -> tuple[int, int]:
    max_x = 0
    max_y = 0
    for node in nodes:
        x, y, w, h = node["bbox"]
        max_x = max(max_x, int(x + w))
        max_y = max(max_y, int(y + h))
    return max(max_x + 120, 1200), max(max_y + 120, 1200)


def _extract_reference_targets(text: str) -> list[str]:
    targets: list[str] = []
    consumed_spans: list[tuple[int, int]] = []

    for match in RANGE_REF_RE.finditer(text):
        prefix_a, start_num, prefix_b, end_num = match.groups()
        if prefix_a != prefix_b:
            continue
        start = int(start_num)
        end = int(end_num)
        if start > end:
            start, end = end, start
        for number in range(start, end + 1):
            targets.append(f"{prefix_a}-{number}")
        consumed_spans.append(match.span())

    def _in_consumed(span: tuple[int, int]) -> bool:
        return any(span[0] >= start and span[1] <= end for start, end in consumed_spans)

    for match in PAGE_REF_RE.finditer(text):
        if _in_consumed(match.span()):
            continue
        targets.append(match.group(1))

    for match in ALIAS_REF_RE.finditer(text):
        if _in_consumed(match.span()):
            continue
        alias = match.group(1)
        if alias not in targets:
            targets.append(alias)

    ordered: list[str] = []
    seen: set[str] = set()
    for item in targets:
        if item not in seen:
            ordered.append(item)
            seen.add(item)
    return ordered


def _validate_reviewed_page(page_code: str, payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    required_top = {"page_code", "page_number", "graph_type", "nodes", "edges"}
    missing_top = sorted(required_top.difference(payload.keys()))
    if missing_top:
        errors.append(f"missing_top_level_fields={missing_top}")

    if payload.get("graph_type") != "directed":
        errors.append(f"graph_type_must_be_directed:{payload.get('graph_type')}")

    nodes = payload.get("nodes", [])
    edges = payload.get("edges", [])
    page_context = payload.get("page_context")

    node_ids = [node.get("id") for node in nodes]
    if len(node_ids) != len(set(node_ids)):
        errors.append("duplicate_node_ids")

    edge_ids = [edge.get("id") for edge in edges]
    if len(edge_ids) != len(set(edge_ids)):
        errors.append("duplicate_edge_ids")

    node_id_set = set(node_ids)
    edge_pairs: set[tuple[str, str]] = set()
    duplicated_pairs: list[tuple[str, str]] = []

    cross_page_texts: dict[str, list[str]] = {}
    for node in nodes:
        node_id = node.get("id")
        node_type = node.get("node_type")
        if node_type not in VALID_NODE_TYPES:
            errors.append(f"invalid_node_type:{node_id}:{node_type}")
        node_label = node.get("node_label")
        if node_label is not None and node_label not in VALID_NODE_LABELS:
            errors.append(f"invalid_node_label:{node_id}:{node_label}")
        text = str(node.get("verbatim_text", "")).strip()
        if not text:
            errors.append(f"empty_node_text:{node_id}")
        bbox = node.get("bbox")
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or bbox[2] <= 0
            or bbox[3] <= 0
        ):
            errors.append(f"invalid_bbox:{node_id}:{bbox}")
        if node_type == "cross_page":
            cross_page_texts.setdefault(text, []).append(node_id)
        elif CROSS_PAGE_TEXT_HINT_RE.search(text):
            warnings.append(f"cross_page_like_text_in_non_cross_page_node:{node_id}")

    for text, ids in cross_page_texts.items():
        if len(ids) > 1:
            warnings.append(f"duplicated_cross_page_text:{text}:{','.join(ids)}")

    for edge in edges:
        edge_id = edge.get("id")
        source_node_id = edge.get("source_node_id")
        target_node_id = edge.get("target_node_id")
        if edge.get("edge_type") not in VALID_EDGE_TYPES:
            errors.append(f"invalid_edge_type:{edge_id}:{edge.get('edge_type')}")
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
            duplicated_pairs.append(pair)
        edge_pairs.add(pair)

    if duplicated_pairs:
        errors.append(f"duplicate_directed_pairs:{duplicated_pairs}")

    indegree = {node_id: 0 for node_id in node_id_set}
    for edge in edges:
        target_node_id = edge.get("target_node_id")
        if target_node_id in indegree:
            indegree[target_node_id] += 1
    entry_anchor_candidates = sorted(
        [
            node["id"]
            for node in nodes
            if node["id"] in indegree
            and indegree[node["id"]] == 0
            and node.get("node_type") != "cross_page"
        ]
    )
    if not entry_anchor_candidates:
        warnings.append("no_non_cross_page_entry_anchor_candidates")

    if page_context is not None:
        for field in ("guideline_header", "page_title", "page_scope_summary"):
            if not str(page_context.get(field, "")).strip():
                errors.append(f"missing_page_context_field:{page_code}:{field}")

    return {
        "page_code": page_code,
        "page_number": payload.get("page_number"),
        "status": "ok" if not errors else "error",
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "entry_anchor_candidates": entry_anchor_candidates,
    }


def _next_page_node_id(page_code: str, used_ids: set[str]) -> str:
    prefix = page_code.replace("-", "")
    index = 1
    while True:
        candidate = f"{prefix}_N{index:02d}"
        if candidate not in used_ids:
            return candidate
        index += 1


def _normalize_reviewed_payload(page_code: str, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    normalized = json.loads(json.dumps(payload))
    notes: list[str] = []
    nodes = normalized.get("nodes", [])
    edges = normalized.get("edges", [])

    used_ids = {node.get("id") for node in nodes if node.get("id")}
    node_id_map: dict[str, str] = {}
    for node in nodes:
        original_id = node.get("id")
        if isinstance(original_id, str) and original_id.startswith("shape:"):
            replacement = _next_page_node_id(page_code, used_ids)
            used_ids.add(replacement)
            node_id_map[original_id] = replacement
            node["id"] = replacement
            notes.append(f"normalized_shape_node_id:{original_id}->{replacement}")

    for edge in edges:
        source_node_id = edge.get("source_node_id")
        target_node_id = edge.get("target_node_id")
        if source_node_id in node_id_map:
            edge["source_node_id"] = node_id_map[source_node_id]
        if target_node_id in node_id_map:
            edge["target_node_id"] = node_id_map[target_node_id]

    return normalized, notes


def _global_layout(page_payloads: list[dict[str, Any]], page_order: list[str]) -> dict[str, Any]:
    page_gap = 320
    current_x = 0
    page_strip: list[dict[str, Any]] = []
    for page_code in page_order:
        payload = next(item for item in page_payloads if item["page_code"] == page_code)
        width, height = _page_size_from_nodes(payload["nodes"])
        page_strip.append(
            {
                "page_code": page_code,
                "page_number": payload["page_number"],
                "page_origin": [current_x, 0],
                "page_size": [width, height],
            }
        )
        current_x += width + page_gap
    return {
        "kind": "page_strip",
        "page_gap": page_gap,
        "page_strip": page_strip,
    }


def _entry_anchors_for_page(
    page_code: str,
    validation_record: dict[str, Any],
    stitch_map: dict[str, Any],
) -> list[str]:
    overrides = stitch_map.get("page_anchor_overrides", {})
    if page_code in overrides:
        return list(overrides[page_code])
    return list(validation_record.get("entry_anchor_candidates", []))


def _external_node_from_alias(alias: str, alias_record: dict[str, Any]) -> dict[str, Any]:
    node_id = alias_record["node_id"]
    return {
        "id": node_id,
        "page_code": "EXTERNAL",
        "page_number": None,
        "local_node_id": alias,
        "node_type": alias_record.get("node_type", "reference"),
        "verbatim_text": alias_record.get("label", alias),
        "text_snippet": alias_record.get("label", alias),
        "page_local_bbox": None,
        "global_bbox": None,
        "is_uncertain": False,
        "why_node": alias_record.get("description", f"External stitch alias {alias}."),
        "source_shape_id": None,
        "origin": "stitch_map_external_alias",
    }


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        normalized = str(item).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _reviewed_footnote_id(page_code: str, label: str) -> str:
    return f"{page_code}:{label}"


def _reference_asset_map(payload: dict[str, Any] | None) -> dict[str, set[str]]:
    by_node: dict[str, set[str]] = {}
    if not payload:
        return by_node
    for asset in payload.get("assets", []):
        asset_id = asset.get("asset_id")
        if not asset_id:
            continue
        for mention in asset.get("mentions", []):
            node_id = mention.get("source_node_id")
            if not node_id:
                continue
            by_node.setdefault(node_id, set()).add(asset_id)
    return by_node


def _reviewed_footnote_map(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    by_node: dict[str, dict[str, Any]] = {}
    if not payload:
        return by_node
    for item in payload.get("nodes", []):
        node_id = item.get("source_node_id")
        if not node_id:
            continue
        by_node[node_id] = item
    return by_node


def _footnote_ref_index(payload: dict[str, Any] | None) -> dict[tuple[str, str], set[str]]:
    by_page_label: dict[tuple[str, str], set[str]] = {}
    if not payload:
        return by_page_label
    for asset in payload.get("assets", []):
        asset_id = asset.get("asset_id")
        if not asset_id:
            continue
        for mention in asset.get("mentions", []):
            page_code = mention.get("source_page_code")
            label = mention.get("footnote_label")
            if not page_code or not label:
                continue
            key = (str(page_code), str(label))
            by_page_label.setdefault(key, set()).add(asset_id)
    return by_page_label


def _page_footnote_bank(payload: dict[str, Any] | None) -> dict[int, dict[str, str]]:
    bank: dict[int, dict[str, str]] = {}
    if not payload:
        return bank
    for item in payload.get("pages", []):
        page_number = item.get("page_number")
        if page_number is None:
            continue
        bank[int(page_number)] = {
            str(footnote.get("label")): str(footnote.get("text", "")).strip()
            for footnote in item.get("footnotes", [])
            if footnote.get("label")
        }
    return bank


def _enrich_global_node(
    node: dict[str, Any],
    *,
    page_context: dict[str, Any] | None,
    explicit_ref_ids: list[str],
    reviewed_footnote_record: dict[str, Any] | None,
    footnote_ref_ids: list[str],
    page_footnote_texts: dict[str, str] | None,
) -> dict[str, Any]:
    enriched = dict(node)
    page_context = page_context or {}
    reviewed_footnote_record = reviewed_footnote_record or {}
    reviewed_footnotes = reviewed_footnote_record.get("footnotes", [])
    reviewed_labels = _dedupe_preserve_order(
        [str(item.get("label", "")).strip() for item in reviewed_footnotes]
    )
    reviewed_ids = [_reviewed_footnote_id(node["page_code"], label) for label in reviewed_labels]
    if page_footnote_texts:
        reviewed_texts = [
            page_footnote_texts[label]
            for label in reviewed_labels
            if label in page_footnote_texts
        ]
    else:
        reviewed_texts = [
            str(item.get("text", "")).strip()
            for item in reviewed_footnotes
            if str(item.get("text", "")).strip()
        ]
    reviewed_texts = _dedupe_preserve_order(reviewed_texts)
    explicit_ref_ids = _dedupe_preserve_order(explicit_ref_ids)
    footnote_ref_ids = _dedupe_preserve_order(footnote_ref_ids)

    enriched["guideline_header"] = page_context.get("guideline_header")
    enriched["page_title"] = page_context.get("page_title")
    enriched["page_scope_summary"] = page_context.get("page_scope_summary")

    enriched["explicit_ref_ids"] = explicit_ref_ids
    enriched["explicit_ref_count"] = len(explicit_ref_ids)
    enriched["has_explicit_refs"] = bool(explicit_ref_ids)

    enriched["reviewed_footnote_ids"] = reviewed_ids
    enriched["reviewed_footnote_labels"] = reviewed_labels
    enriched["reviewed_footnote_texts"] = reviewed_texts
    enriched["reviewed_footnote_count"] = len(reviewed_ids)
    enriched["has_reviewed_footnotes"] = bool(reviewed_ids)

    enriched["reviewed_footnote_ref_ids"] = footnote_ref_ids
    enriched["reviewed_footnote_ref_count"] = len(footnote_ref_ids)
    enriched["has_reviewed_footnote_refs"] = bool(footnote_ref_ids)
    return enriched


def build_reviewed_global_graph(
    *,
    input_root: Path | None = None,
    stitch_map_path: Path | None = None,
    page_footnotes_path: Path | None = None,
    reference_assets_path: Path | None = None,
    footnote_reference_assets_path: Path | None = None,
    reviewed_footnote_links_path: Path | None = None,
    page_filename: str = "page_graph.typed.json",
    graph_ir_root: Path | None = None,
    reviewed_graph_root: Path | None = None,
    report_root: Path | None = None,
    release_root: Path | None = None,
) -> dict[str, Any]:
    roots = ov_2025_roots()
    input_root = input_root or (roots["processed_root"] / "pages")
    stitch_map_path = stitch_map_path or (REPO_ROOT / "data" / "manifests" / "ov_2025_stitch_map.json")
    page_footnotes_path = page_footnotes_path or (roots["processed_root"] / "text" / "ov_2025_page_footnotes.json")
    reference_assets_path = reference_assets_path or (roots["processed_root"] / "text" / "ov_2025_reference_assets.json")
    footnote_reference_assets_path = footnote_reference_assets_path or (roots["processed_root"] / "text" / "ov_2025_footnote_reference_assets.json")
    reviewed_footnote_links_path = reviewed_footnote_links_path or (roots["processed_root"] / "text" / "ov_2025_reviewed_footnote_links.json")
    graph_ir_root = graph_ir_root or roots["graph_ir"]
    reviewed_graph_root = reviewed_graph_root or roots["reviewed_graph"]
    report_root = report_root or roots["reports"]
    release_root = release_root or roots["freeze_root"]

    stitch_map = _load_stitch_map(stitch_map_path)
    page_footnotes_payload = _load_optional_json(page_footnotes_path)
    reference_assets_payload = _load_optional_json(reference_assets_path)
    footnote_reference_assets_payload = _load_optional_json(footnote_reference_assets_path)
    reviewed_footnote_links_payload = _load_optional_json(reviewed_footnote_links_path)
    explicit_refs_by_node = _reference_asset_map(reference_assets_payload)
    reviewed_footnotes_by_node = _reviewed_footnote_map(reviewed_footnote_links_payload)
    footnote_refs_by_page_label = _footnote_ref_index(footnote_reference_assets_payload)
    page_footnotes_by_number = _page_footnote_bank(page_footnotes_payload)
    page_order = list(stitch_map["page_order"])

    validation_records: list[dict[str, Any]] = []
    page_payloads: list[dict[str, Any]] = []
    for page_code in page_order:
        reviewed_path = _page_graph_path(page_code, input_root, page_filename)
        if not reviewed_path.exists():
            validation_records.append(
                {
                    "page_code": page_code,
                    "status": "error",
                    "errors": [f"missing_reviewed_page:{reviewed_path}"],
                    "warnings": [],
                    "node_count": 0,
                    "edge_count": 0,
                    "entry_anchor_candidates": [],
                }
            )
            continue
        payload = _read_json(reviewed_path)
        payload, normalization_notes = _normalize_reviewed_payload(page_code, payload)
        page_payloads.append(payload)
        record = _validate_reviewed_page(page_code, payload)
        if normalization_notes:
            record["warnings"].extend(normalization_notes)
            record["warning_count"] = len(record["warnings"])
        validation_records.append(record)

    validation_summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "input_root": str(input_root),
        "page_filename": page_filename,
        "stitch_map_path": str(stitch_map_path),
        "pages": validation_records,
        "error_count": sum(item["error_count"] for item in validation_records if "error_count" in item),
        "warning_count": sum(item["warning_count"] for item in validation_records if "warning_count" in item),
    }
    validation_report_path = report_root / "ov_2025_reviewed_page_validation.json"
    _write_json(validation_report_path, validation_summary)

    blocking_pages = [item for item in validation_records if item["status"] != "ok"]
    if blocking_pages:
        return {
            "status": "error",
            "stage": "validation",
            "validation_report_path": str(validation_report_path),
            "blocking_pages": [item["page_code"] for item in blocking_pages],
        }

    page_payload_by_code = {payload["page_code"]: payload for payload in page_payloads}
    validation_by_page = {item["page_code"]: item for item in validation_records}
    layout = _global_layout(page_payloads, page_order)
    page_origins = {item["page_code"]: item["page_origin"] for item in layout["page_strip"]}

    global_nodes: list[dict[str, Any]] = []
    global_edges: list[dict[str, Any]] = []
    node_lookup: dict[str, dict[str, Any]] = {}
    for payload in page_payloads:
        page_code = payload["page_code"]
        origin_x, origin_y = page_origins[page_code]
        for node in payload["nodes"]:
            x, y, w, h = node["bbox"]
            global_node = {
                "id": node["id"],
                "page_code": page_code,
                "page_number": payload["page_number"],
                "local_node_id": node["id"],
                "node_type": node["node_type"],
                "node_label": node.get("node_label"),
                "verbatim_text": node["verbatim_text"],
                "text_snippet": node["text_snippet"],
                "page_local_bbox": list(node["bbox"]),
                "global_bbox": [origin_x + x, origin_y + y, w, h],
                "is_uncertain": bool(node.get("is_uncertain", False)),
                "why_node": node.get("why_node", ""),
                "source_shape_id": node.get("shape_id"),
                "origin": "reviewed_page",
            }
            reviewed_record = reviewed_footnotes_by_node.get(global_node["id"])
            reviewed_labels = []
            if reviewed_record:
                reviewed_labels = _dedupe_preserve_order(
                    [
                        str(item.get("label", "")).strip()
                        for item in reviewed_record.get("footnotes", [])
                    ]
                )
            footnote_ref_ids = _dedupe_preserve_order(
                [
                    asset_id
                    for label in reviewed_labels
                    for asset_id in sorted(
                        footnote_refs_by_page_label.get((page_code, label), set())
                    )
                ]
            )
            global_node = _enrich_global_node(
                global_node,
                page_context=payload.get("page_context"),
                explicit_ref_ids=sorted(explicit_refs_by_node.get(global_node["id"], set())),
                reviewed_footnote_record=reviewed_record,
                footnote_ref_ids=footnote_ref_ids,
                page_footnote_texts=page_footnotes_by_number.get(payload["page_number"]),
            )
            global_nodes.append(global_node)
            node_lookup[global_node["id"]] = global_node
        for edge in payload["edges"]:
            global_edges.append(
                {
                    "id": edge["id"],
                    "source_node_id": edge["source_node_id"],
                    "target_node_id": edge["target_node_id"],
                    "source_page_code": page_code,
                    "target_page_code": page_code,
                    "edge_type": edge["edge_type"],
                    "edge_label": edge.get("edge_label"),
                    "is_uncertain": bool(edge.get("is_uncertain", False)),
                    "why_edge": edge.get("why_edge", ""),
                    "stitch_kind": "intra_page",
                    "local_edge_id": edge["id"],
                    "source_shape_id": edge.get("shape_id"),
                }
            )

    external_nodes: dict[str, dict[str, Any]] = {}
    stitch_resolution_records: list[dict[str, Any]] = []
    unresolved_refs: list[dict[str, Any]] = []
    cross_page_edges: list[dict[str, Any]] = []
    external_aliases = stitch_map.get("external_aliases", {})

    for payload in page_payloads:
        page_code = payload["page_code"]
        page_edges = payload["edges"]
        node_by_id = {node["id"]: node for node in payload["nodes"]}
        inbound_by_target: dict[str, list[dict[str, Any]]] = {}
        for edge in page_edges:
            inbound_by_target.setdefault(edge["target_node_id"], []).append(edge)

        for node in payload["nodes"]:
            if node["node_type"] != "cross_page":
                continue
            targets = _extract_reference_targets(node["verbatim_text"])
            source_edges = inbound_by_target.get(node["id"], [])
            resolution = {
                "source_page_code": page_code,
                "cross_page_node_id": node["id"],
                "cross_page_text": node["verbatim_text"],
                "source_edge_ids": [edge["id"] for edge in source_edges],
                "targets": targets,
                "resolved_targets": [],
            }
            if not targets:
                unresolved_refs.append(
                    {
                        "source_page_code": page_code,
                        "cross_page_node_id": node["id"],
                        "reason": "no_target_detected",
                        "text": node["verbatim_text"],
                    }
                )
                stitch_resolution_records.append(resolution)
                continue

            for target in targets:
                if target in page_payload_by_code:
                    anchors = _entry_anchors_for_page(target, validation_by_page[target], stitch_map)
                    if not anchors:
                        unresolved_refs.append(
                            {
                                "source_page_code": page_code,
                                "cross_page_node_id": node["id"],
                                "reason": "missing_entry_anchor",
                                "target": target,
                            }
                        )
                        continue
                    resolution["resolved_targets"].append({"target": target, "kind": "page", "anchors": anchors})
                    for anchor in anchors:
                        cross_page_edges.append(
                            {
                                "id": f"STITCH__{node['id']}__{anchor}",
                                "source_node_id": node["id"],
                                "target_node_id": anchor,
                                "source_page_code": page_code,
                                "target_page_code": target,
                                "edge_type": "cross_page_ref",
                                "edge_label": node["verbatim_text"],
                                "is_uncertain": False,
                                "why_edge": f"Stitched from cross_page node {node['id']} to {target}.",
                                "stitch_kind": "cross_page",
                                "local_edge_id": None,
                                "source_shape_id": node.get("shape_id"),
                            }
                        )
                elif target in external_aliases:
                    alias_record = external_aliases[target]
                    external_nodes.setdefault(target, _external_node_from_alias(target, alias_record))
                    resolution["resolved_targets"].append({"target": target, "kind": "external_alias", "anchors": [alias_record["node_id"]]})
                    cross_page_edges.append(
                        {
                            "id": f"STITCH__{node['id']}__{alias_record['node_id']}",
                            "source_node_id": node["id"],
                            "target_node_id": alias_record["node_id"],
                            "source_page_code": page_code,
                            "target_page_code": "EXTERNAL",
                            "edge_type": "cross_page_ref",
                            "edge_label": node["verbatim_text"],
                            "is_uncertain": False,
                            "why_edge": f"Stitched from cross_page node {node['id']} to external alias {target}.",
                            "stitch_kind": "external_ref",
                            "local_edge_id": None,
                            "source_shape_id": node.get("shape_id"),
                        }
                    )
                else:
                    unresolved_refs.append(
                        {
                            "source_page_code": page_code,
                            "cross_page_node_id": node["id"],
                            "reason": "unmapped_target",
                            "target": target,
                            "text": node["verbatim_text"],
                        }
                    )
            stitch_resolution_records.append(resolution)

    global_nodes.extend(external_nodes.values())
    global_edges.extend(cross_page_edges)

    graph_ir_payload = {
        "graph_id": "ov_2025_global",
        "guideline": "NCCN_OV_2025",
        "version": "2025",
        "graph_type": "directed",
        "page_order": page_order,
        "pages": [
            {
                "page_code": payload["page_code"],
                "page_number": payload["page_number"],
                "node_count": len(payload["nodes"]),
                "edge_count": len(payload["edges"]),
                "entry_anchors": _entry_anchors_for_page(payload["page_code"], validation_by_page[payload["page_code"]], stitch_map),
            }
            for payload in page_payloads
        ],
        "nodes": global_nodes,
        "edges": global_edges,
        "cross_page_resolution": {
            "records": stitch_resolution_records,
            "unresolved": unresolved_refs,
        },
        "layout": layout,
    }
    graph_ir_path = graph_ir_root / "ov_2025_global.graph_ir.json"
    _write_json(graph_ir_path, graph_ir_payload)

    duplicate_global_nodes = len({node["id"] for node in global_nodes}) != len(global_nodes)
    edge_pairs = [(edge["source_node_id"], edge["target_node_id"]) for edge in global_edges]
    duplicate_global_edges = len(set(edge_pairs)) != len(edge_pairs)
    stitch_report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "graph_ir_path": str(graph_ir_path),
        "validation_report_path": str(validation_report_path),
        "page_count": len(page_order),
        "node_count": len(global_nodes),
        "edge_count": len(global_edges),
        "cross_page_edge_count": len(cross_page_edges),
        "external_node_count": len(external_nodes),
        "unresolved_ref_count": len(unresolved_refs),
        "duplicate_global_nodes": duplicate_global_nodes,
        "duplicate_global_edges": duplicate_global_edges,
        "status": "ok",
    }
    if unresolved_refs or duplicate_global_nodes or duplicate_global_edges:
        stitch_report["status"] = "error"
    stitch_report_path = report_root / "ov_2025_global_stitch_report.json"
    _write_json(stitch_report_path, stitch_report)

    if stitch_report["status"] != "ok":
        return {
            "status": "error",
            "stage": "stitch",
            "graph_ir_path": str(graph_ir_path),
            "validation_report_path": str(validation_report_path),
            "stitch_report_path": str(stitch_report_path),
            "unresolved_ref_count": len(unresolved_refs),
        }

    reviewed_graph_payload = {
        "graph_id": "ov_2025_global",
        "guideline": "NCCN_OV_2025",
        "version": "2025",
        "graph_type": "directed",
        "status": "reviewed_global",
        "page_order": page_order,
        "nodes": global_nodes,
        "edges": global_edges,
        "layout": layout,
        "source_pages": [
            {
                "page_code": payload["page_code"],
                "page_number": payload["page_number"],
                "typed_path": str(_page_graph_path(payload["page_code"], input_root, page_filename)),
                "page_context": payload.get("page_context"),
            }
            for payload in page_payloads
        ],
    }
    reviewed_graph_path = reviewed_graph_root / "ov_2025_global.reviewed_graph.json"
    _write_json(reviewed_graph_path, reviewed_graph_payload)

    summary_payload = {
        "graph_id": "ov_2025_global",
        "status": "reviewed_global",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "page_count": len(page_order),
        "node_count": len(global_nodes),
        "edge_count": len(global_edges),
        "cross_page_edge_count": len(cross_page_edges),
        "external_node_count": len(external_nodes),
        "nodes_with_explicit_refs": sum(1 for node in global_nodes if node.get("has_explicit_refs")),
        "nodes_with_reviewed_footnotes": sum(1 for node in global_nodes if node.get("has_reviewed_footnotes")),
        "nodes_with_reviewed_footnote_refs": sum(1 for node in global_nodes if node.get("has_reviewed_footnote_refs")),
        "graph_ir_path": str(graph_ir_path),
        "reviewed_graph_path": str(reviewed_graph_path),
        "validation_report_path": str(validation_report_path),
        "stitch_report_path": str(stitch_report_path),
        "page_footnotes_path": str(page_footnotes_path),
        "reference_assets_path": str(reference_assets_path),
        "footnote_reference_assets_path": str(footnote_reference_assets_path),
        "reviewed_footnote_links_path": str(reviewed_footnote_links_path),
    }
    summary_path = reviewed_graph_root / "ov_2025_global.summary.json"
    _write_json(summary_path, summary_payload)

    freeze_name = f"ov_2025_global_graph_freeze_{date.today().isoformat()}"
    freeze_dir = release_root / freeze_name
    freeze_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(reviewed_graph_path, freeze_dir / reviewed_graph_path.name)
    shutil.copy2(summary_path, freeze_dir / summary_path.name)
    shutil.copy2(stitch_report_path, freeze_dir / stitch_report_path.name)

    frozen_files = []
    for path in sorted(freeze_dir.iterdir()):
        if path.is_file():
            frozen_files.append(
                {
                    "path": path.name,
                    "size_bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    freeze_manifest = {
        "freeze_name": freeze_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_reviewed_graph": str(reviewed_graph_path),
        "files": frozen_files,
    }
    _write_json(freeze_dir / "manifest.json", freeze_manifest)
    (freeze_dir / "README.md").write_text(
        "# OV 2025 Global Graph Freeze\n\n"
        "This directory is a frozen snapshot of the stitched OV 2025 global graph.\n\n"
        f"- graph_id: {summary_payload['graph_id']}\n"
        f"- page_count: {summary_payload['page_count']}\n"
        f"- node_count: {summary_payload['node_count']}\n"
        f"- edge_count: {summary_payload['edge_count']}\n"
        f"- cross_page_edge_count: {summary_payload['cross_page_edge_count']}\n\n"
        "See `manifest.json` for checksums.\n"
    )

    return {
        "status": "ok",
        "validation_report_path": str(validation_report_path),
        "graph_ir_path": str(graph_ir_path),
        "stitch_report_path": str(stitch_report_path),
        "reviewed_graph_path": str(reviewed_graph_path),
        "summary_path": str(summary_path),
        "freeze_dir": str(freeze_dir),
        "page_count": len(page_order),
        "node_count": len(global_nodes),
        "edge_count": len(global_edges),
        "cross_page_edge_count": len(cross_page_edges),
    }
